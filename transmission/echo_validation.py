"""
Echo validation + adaptive rerouting — Phase 6

Protocol
--------
After Phase 5 reassembly, Node B encrypts and echoes the full reassembled
payload back to Node A over the existing classical DataChannel.
Node A compares the echo against the original payload byte-for-byte.

Happy path (overwhelmingly common):
    Echo == original → log success (INFO), done.

Mismatch path:
    1. Find contiguous byte range(s) where original and echo differ.
    2. Classify which segment the mismatch falls in, using the quantum_len
       boundary already computed by Phase 5's payload_splitter:
         [0, quantum_len)       → "quantum" segment
         [quantum_len, total)   → "classical" segment
         spanning both          → "both"
       Note: AES-GCM already authenticates the classical segment at decrypt
       time; any classical tampering raises InvalidTag before we ever reach
       this stage.  Silent corruption here is almost always quantum-originated.
    3. Diagnose:
         QBER > ANOMALY_THRESHOLD (0.05) → QUANTUM_CHANNEL_ISSUE
           (channel degraded but below the 0.11 session-abort threshold)
         QBER ≤ ANOMALY_THRESHOLD        → POSSIBLE_EAVESDROPPER
           (low noise + silent corruption is anomalous; logged at CRITICAL
           via a dedicated "echo_validation.SECURITY" logger so it is
           visually unmistakable in the log stream)
    4. Retransmit ONLY the mismatched byte range(s) over the classical
       AES-GCM channel (not the full payload, not the quantum channel).
    5. Bob applies the patch and echoes the patched range back.
    6. Alice verifies the re-echo.
         Match  → EchoResult(success=True, recovered=True) — RECOVERED_VIA_REROUTE
         Still bad → raise ChannelFailureError (one retry, then abort)

Wire protocol (all frames over classical_dc, all AES-GCM encrypted):
    Bob  → Alice : encrypt(full_reassembled_payload)       [initial echo]
    Alice → Bob  : encrypt(b'{"status":"OK"}')             [clean pass]
                OR encrypt(json_patch_bytes)                [mismatch]
                   where json = {"status":"PATCH",
                                 "patches":[[start,end,hex],...]}
    Bob  → Alice : encrypt(concat_patched_ranges_bytes)    [re-echo; PATCH only]
"""

from __future__ import annotations

import json
import logging
import sys
import pathlib
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from classical.aes_channel import encrypt, decrypt
from metrics.logger import get_logger

__all__ = [
    "ANOMALY_THRESHOLD",
    "EchoResult",
    "ChannelFailureError",
    "alice_transfer",
    "bob_transfer",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ANOMALY_THRESHOLD: float = 0.05
"""
QBER threshold for mismatch diagnosis — below the 0.11 session-abort floor.

  QBER ≤ 0.05  + mismatch → POSSIBLE_EAVESDROPPER   (suspicious: low noise, still corrupt)
  0.05 < QBER ≤ 0.11      → QUANTUM_CHANNEL_ISSUE    (degraded channel, not yet aborting)
  QBER > 0.11             → session aborted by Phase 2, never reaches Phase 6
"""

# ---------------------------------------------------------------------------
# Loggers
# ---------------------------------------------------------------------------

logger = get_logger("echo_validation")

# Security events use a *separate* named logger at CRITICAL level.
# In the JSON log stream this produces lines with
#   "levelname": "CRITICAL"  and  "name": "echo_validation.SECURITY"
# — visually unmistakable compared to the INFO / WARNING lines from the
# main logger above.
_security_logger = get_logger("echo_validation.SECURITY", level=logging.CRITICAL)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ChannelFailureError(Exception):
    """Raised when echo mismatch persists after one retransmit attempt."""

    def __init__(self, ranges: list[tuple[int, int]], diagnosis: str) -> None:
        self.ranges = ranges
        self.diagnosis = diagnosis
        super().__init__(
            f"Channel failure after reroute attempt "
            f"(diagnosis={diagnosis}, bad_ranges={ranges})"
        )


@dataclass
class EchoResult:
    """Outcome of the alice_transfer() call."""

    success:           bool
    recovered:         bool                      # True iff success came via reroute
    mismatched_ranges: list[tuple[int, int]] = field(default_factory=list)
    mismatch_source:   str | None = None         # "quantum" | "classical" | "both" | None
    echo_diagnosis:    str | None = None         # "QUANTUM_CHANNEL_ISSUE" | "POSSIBLE_EAVESDROPPER" | None
    retransmit_bytes:  int = 0                   # bytes sent on retry (0 on clean pass)


# ---------------------------------------------------------------------------
# Public orchestration — call these from application code
# ---------------------------------------------------------------------------


def alice_transfer(
    node_a,
    payload: bytes,
    decision,           # SplitDecision from Phase 4
    classical_dc,       # DataChannel — used for Phase 5 data AND Phase 6 echo
    quantum_dc,         # DataChannel — Phase 5 quantum data only
    aes_key: bytes,
    available_ebits: int,
    qber: float,
) -> EchoResult:
    """
    Alice: transmit payload (Phase 5) then run echo-validation (Phase 6).

    From the caller's perspective this is one call that either succeeds
    (possibly after an internal reroute) or raises ChannelFailureError.
    """
    from transmission.payload_splitter import split_payload

    node_a.transmit_payload(
        payload, decision, classical_dc, quantum_dc, aes_key, available_ebits
    )

    split = split_payload(payload, decision)
    return _alice_echo_phase(payload, split.quantum_len, classical_dc, aes_key, qber)


def bob_transfer(
    node_b,
    classical_dc,
    quantum_dc,
    aes_key: bytes,
) -> bytes:
    """
    Bob: receive payload (Phase 5) then run echo-validation (Phase 6).

    Returns the final payload (original or patched after rerouting).
    Also updates node_b.last_received_payload to the final value.
    """
    node_b.receive_payload(classical_dc, quantum_dc, aes_key)
    final = _bob_echo_phase(node_b.last_received_payload, classical_dc, aes_key)
    node_b.last_received_payload = final
    return final


# ---------------------------------------------------------------------------
# Internal — Alice-side echo protocol
# ---------------------------------------------------------------------------


def _alice_echo_phase(
    original: bytes,
    quantum_len: int,
    dc,
    aes_key: bytes,
    qber: float,
) -> EchoResult:
    """Receive Bob's echo, compare, and reroute if needed (Alice side)."""

    # Step 1 — receive Bob's encrypted echo of his reassembled payload
    echo = decrypt(dc.recv(), aes_key)

    # Step 2 — byte-for-byte comparison
    ranges = _find_mismatch_ranges(original, echo)

    if not ranges:
        # Happy path: tell Bob we're done, log success
        dc.send(encrypt(b'{"status":"OK"}', aes_key))
        logger.info("Echo validation: clean first-pass match", extra={
            "total_bytes": len(original),
            "qber": qber,
            "outcome": "CLEAN_PASS",
        })
        return EchoResult(success=True, recovered=False)

    # Step 3 — classify mismatch source and diagnose
    source = _classify_source(ranges, quantum_len, len(original))
    diagnosis = (
        "QUANTUM_CHANNEL_ISSUE" if qber > ANOMALY_THRESHOLD
        else "POSSIBLE_EAVESDROPPER"
    )
    _log_mismatch(ranges, source, diagnosis, qber, len(original))

    # Step 4 — send patch: only the mismatched byte range(s), classical only
    patch_msg = {
        "status": "PATCH",
        "patches": [[s, e, original[s:e].hex()] for s, e in ranges],
    }
    dc.send(encrypt(json.dumps(patch_msg).encode(), aes_key))
    retransmit_bytes = sum(e - s for s, e in ranges)

    # Step 5 — receive Bob's re-echo of the patched range(s)
    re_echo = decrypt(dc.recv(), aes_key)
    expected_re_echo = b"".join(original[s:e] for s, e in ranges)

    if re_echo == expected_re_echo:
        logger.info("Echo validation: recovered via reroute", extra={
            "diagnosis": diagnosis,
            "mismatch_source": source,
            "ranges": [list(r) for r in ranges],
            "retransmit_bytes": retransmit_bytes,
            "outcome": "RECOVERED_VIA_REROUTE",
        })
        return EchoResult(
            success=True,
            recovered=True,
            mismatched_ranges=ranges,
            mismatch_source=source,
            echo_diagnosis=diagnosis,
            retransmit_bytes=retransmit_bytes,
        )

    # Step 6 — one retry exhausted; abort
    raise ChannelFailureError(ranges=ranges, diagnosis=diagnosis)


# ---------------------------------------------------------------------------
# Internal — Bob-side echo protocol
# ---------------------------------------------------------------------------


def _bob_echo_phase(received: bytes, dc, aes_key: bytes) -> bytes:
    """Send echo, handle OK or PATCH from Alice, return final payload (Bob side)."""

    # Step 1 — send encrypted echo of full reassembled payload
    dc.send(encrypt(received, aes_key))

    # Step 2 — receive Alice's status frame
    response = json.loads(decrypt(dc.recv(), aes_key).decode())

    if response["status"] == "OK":
        return received

    # Apply patches from Alice's correction frame
    patched = bytearray(received)
    for start, end, hexdata in response["patches"]:
        patched[start:end] = bytes.fromhex(hexdata)
    patched = bytes(patched)

    # Step 3 — re-echo only the patched range(s) so Alice can verify
    re_echo = b"".join(patched[s:e] for s, e, _ in response["patches"])
    dc.send(encrypt(re_echo, aes_key))

    return patched


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_mismatch_ranges(a: bytes, b: bytes) -> list[tuple[int, int]]:
    """Return list of contiguous [start, end) byte ranges where a and b differ."""
    length = min(len(a), len(b))
    ranges: list[tuple[int, int]] = []
    in_mismatch = False
    start = 0
    for i in range(length):
        if a[i] != b[i]:
            if not in_mismatch:
                start = i
                in_mismatch = True
        else:
            if in_mismatch:
                ranges.append((start, i))
                in_mismatch = False
    if in_mismatch:
        ranges.append((start, length))
    # Length difference: the trailing tail is also a mismatch
    if len(a) != len(b):
        tail_start = length
        if ranges and ranges[-1][1] == tail_start:
            ranges[-1] = (ranges[-1][0], max(len(a), len(b)))
        else:
            ranges.append((tail_start, max(len(a), len(b))))
    return ranges


def _classify_source(
    ranges: list[tuple[int, int]], quantum_len: int, total_len: int
) -> str:
    """Return "quantum", "classical", or "both" based on where mismatches fall."""
    in_quantum   = any(s < quantum_len for s, e in ranges)
    in_classical = any(e > quantum_len for s, e in ranges)
    if in_quantum and in_classical:
        return "both"
    return "quantum" if in_quantum else "classical"


def _log_mismatch(
    ranges: list[tuple[int, int]],
    source: str,
    diagnosis: str,
    qber: float,
    total_len: int,
) -> None:
    extra = {
        "diagnosis": diagnosis,
        "mismatch_source": source,
        "ranges": [list(r) for r in ranges],
        "qber": qber,
        "total_len": total_len,
        "mismatched_bytes": sum(e - s for s, e in ranges),
    }
    if diagnosis == "QUANTUM_CHANNEL_ISSUE":
        logger.warning("Echo mismatch: degraded quantum channel", extra=extra)
    else:
        # CRITICAL level + dedicated logger name = stands out unmistakably
        _security_logger.critical(
            "SECURITY EVENT: echo mismatch with low QBER — possible eavesdropper",
            extra=extra,
        )
