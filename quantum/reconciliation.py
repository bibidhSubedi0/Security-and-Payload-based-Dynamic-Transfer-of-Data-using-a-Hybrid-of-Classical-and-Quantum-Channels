r"""
Single-Pass Parity-Based Key Reconciliation for BB84 QKD
=========================================================

-------
Purpose
-------
After sifting and QBER checking, Alice and Bob share nominally the same raw key
bits, but channel noise introduces bit errors that survive the QBER check when
the measured QBER is below the 0.11 abort threshold. These residual errors cause
AES-GCM InvalidTag failures during payload transfer. This module corrects
single bit errors per block before HKDF key derivation, substantially reducing
the key-mismatch rate.

------------------
Algorithm Overview
------------------
1. Block size selection: block_size = max(1, floor(0.73 / max(QBER, epsilon))).
   This is the standard Cascade pass-1 heuristic: at error rate p, the expected
   errors per block approx 0.73, maximising single-pass efficiency. epsilon = 1e-4
   avoids division by zero in the noiseless case.

2. Block partition: Alice's and Bob's key bits (post-sifting, pre-HKDF) are
   split into consecutive blocks of block_size (last block may be shorter).

3. Parity exchange: Alice sends all block parities in one message. Bob
   computes his own parities, identifies mismatched blocks, and sends back the
   mismatch indices.

4. Binary search correction: For each mismatched block Alice and Bob run a
   bisection protocol (Alice sends sub-block parities, Bob navigates left/right,
   Alice finally sends the correct bit value at the isolated position). Bob
   corrects his bit.

5. Privacy amplification (simplified): After reconciliation, both sides
   discard parity_bits_revealed bits from the front of the key. This is an
   approximation of full privacy amplification: a rigorous implementation uses a
   universal hash function (e.g., Toeplitz) sized to the actual Shannon leakage.
   The approximation is acceptable here; the remaining bits are further processed
   by HKDF-SHA256, which provides additional extraction.

6. Key verification: After deriving the final AES key via HKDF, Alice and
   Bob exchange a short AES-GCM authenticated ping/pong. If decryption fails,
   ReconciliationIncompleteError is raised immediately which is a clear, diagnosable
   failure instead of a silent InvalidTag buried deep in the payload transfer.

------------------------------------
Known Limitation - Single-Pass Only
------------------------------------
A block containing an EVEN number of errors passes its parity check silently.
The errors remain in the reconciled key and are NOT corrected. This is a known,
accepted limitation of single-pass reconciliation. Full multi-pass Cascade with
random block reshuffling between passes would catch these cases but is out of
scope for this project. The limitation is explicitly tested in
tests/test_reconciliation.py (test_even_error_count_undetected).

----------------------
Channel Security Model
----------------------
Reconciliation messages are encrypted and authenticated with AES-256-GCM using a
fixed project-level constant key (_RECON_AUTH_KEY). This key is NOT the
session key -- it is embedded in the source and therefore not secret. This is
acceptable because:
  (a) parity bits are discarded in privacy amplification, so leaking them to a
      passive eavesdropper provides at most 1 bit of key information per block;
  (b) the session key itself is not transmitted during reconciliation;
  (c) AES-GCM authentication protects against channel corruption and replay,
      which is the primary threat in this simulation context.
Using a constant auth key avoids the chicken-and-egg problem of deriving an
auth key from the (not-yet-agreed-upon) session bits.

----------
Public API
----------
block_size_for_qber(qber)          -> block size integer
reconcile_alice(bits, qber, dc)    -> ReconcileResult
reconcile_bob(bits, qber, dc)      -> ReconcileResult
verify_key_alice(dc, aes_key)      -> None (raises ReconciliationIncompleteError)
verify_key_bob(dc, aes_key)        -> None (raises ReconciliationIncompleteError)
ReconcileResult                    dataclass
ReconciliationIncompleteError      exception
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import pathlib
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from classical.aes_channel import encrypt, decrypt, InvalidTag

__all__ = [
    "block_size_for_qber",
    "reconcile_alice",
    "reconcile_bob",
    "verify_key_alice",
    "verify_key_bob",
    "ReconcileResult",
    "ReconciliationIncompleteError",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fixed auth key for reconciliation channel messages only.
# NOT the session key. See module docstring for rationale.
_RECON_AUTH_KEY: bytes = hashlib.sha256(b"hbd-class-quant-recon-v1").digest()

EPSILON: float = 1e-4  # Avoid division by zero at QBER = 0


# ---------------------------------------------------------------------------
# Public Types
# ---------------------------------------------------------------------------


class ReconciliationIncompleteError(Exception):
    r"""
    Raised when key verification fails after reconciliation.

    Indicates that at least one block contained an even number of errors
    (the documented single-pass limitation), leaving the reconciled keys
    still different. The caller should treat this as a session failure
    distinct from KEY_MISMATCH_ERROR (which occurs without reconciliation).
    """


@dataclass
class ReconcileResult:
    r"""Return value of reconcile_alice / reconcile_bob."""

    reconciled_bits: list[int]  # bits remaining after correction + PA trim
    bits_corrected: int          # errors corrected (Bob's view; always 0 for Alice)
    bits_sacrificed: int         # bits discarded for privacy amplification


# ---------------------------------------------------------------------------
# Public Helpers
# ---------------------------------------------------------------------------


def block_size_for_qber(qber: float) -> int:
    r"""
    Cascade pass-1 heuristic: block_size = floor(0.73 / max(qber, EPSILON)).

    At the returned size, the expected number of errors per block approx 0.73,
    which maximises single-pass detection efficiency. The result is capped at
    1 so it is always >= 1.

    ----------
    Parameters
    ----------
    qber : float
        Quantum Bit Error Rate measured during check-bit phase. Range [0, 1].

    -------
    Returns
    -------
    int
        Optimal block size for single-pass reconciliation at this QBER.
        For qber=0.01 -> 73. For qber=0.05 -> 14. For qber=0.10 -> 7.
    """
    return max(1, math.floor(0.73 / max(qber, EPSILON)))


# ---------------------------------------------------------------------------
# Core Reconciliation - Alice (Reference) Side
# ---------------------------------------------------------------------------


def reconcile_alice(
    raw_bits: list[int],
    qber: float,
    dc,
) -> ReconcileResult:
    r"""
    Run single-pass reconciliation - Alice (authoritative reference) side.

    ----------
    Parameters
    ----------
    raw_bits : list[int]
        Alice's raw sifted key bits (after check-bit split, before HKDF).
    qber : float
        QBER measured during the check-bit phase (used for block sizing).
    dc : DataChannel
        Classical data channel - used BEFORE the payload transfer begins, on
        the same classical socket. Caller must ensure the channel is connected.

    -------
    Returns
    -------
    ReconcileResult
        reconciled_bits ready for derive_key().
        bits_corrected is always 0 (Alice is the reference).
        bits_sacrificed = number of parity bits revealed during protocol.

    --------------
    Protocol Steps
    --------------
    1. Partition raw_bits into blocks of size block_size_for_qber(qber).
    2. Send all block parities to Bob (encrypted with _RECON_AUTH_KEY).
    3. Receive mismatch_indices from Bob (blocks where parity differed).
    4. For each mismatched block, run binary search:
         - Send sub-block parity, Bob responds L/R
         - Repeat until single-bit position isolated
         - Send correct bit value at that position
    5. Send sacrificed count (total parity bits revealed).
    6. Both sides trim sacrificed bits from front of key (privacy amplification).
    """
    bits = list(raw_bits)
    n = len(bits)
    if n == 0:
        # Consume the expected protocol messages so Bob can drain cleanly
        _enc_send(dc, {"type": "PARITIES", "parities": [], "bsz": 1, "n": 0})
        _dec_recv(dc)   # mismatch_indices
        _enc_send(dc, {"type": "DONE", "sacrificed": 0})
        return ReconcileResult(reconciled_bits=[], bits_corrected=0, bits_sacrificed=0)

    bsz = min(block_size_for_qber(qber), n)
    blocks = _make_blocks(n, bsz)

    # Step 1: send all block parities
    parities = [_parity(bits, s, e) for s, e in blocks]
    _enc_send(dc, {"type": "PARITIES", "parities": parities, "bsz": bsz, "n": n})

    # Step 2: receive mismatch indices from Bob
    mismatch_indices: list[int] = _dec_recv(dc)["mismatch_indices"]

    parity_bits_revealed = len(blocks)  # one bit per block already revealed

    # Step 3: binary search + correction for each mismatched block
    for bidx in mismatch_indices:
        s, e = blocks[bidx]
        lo, hi = s, e
        max_depth = math.ceil(math.log2(max(e - s, 2))) + 2  # hard cap: log2(block) + slack
        depth = 0

        # Bisect until the block contains exactly one bit
        while hi - lo > 1:
            depth += 1
            if depth > max_depth:
                raise RuntimeError(
                    f"Bisect depth {depth} exceeded cap {max_depth}: "
                    f"block={bidx} lo={lo} hi={hi} bsz={bsz} - logic error"
                )
            mid = (lo + hi) // 2
            _enc_send(dc, {"type": "SUBPARITY", "sp": _parity(bits, lo, mid)})
            parity_bits_revealed += 1
            go = _dec_recv(dc)["go"]
            if go == "L":
                hi = mid
            else:
                lo = mid

        # lo is the isolated error position; Alice's value is authoritative
        _enc_send(dc, {"type": "CORRECT", "pos": lo, "val": bits[lo]})
        parity_bits_revealed += 1  # the correct bit value is revealed

    # Step 4: privacy amplification -- trim revealed bits from the front
    sacrificed = min(parity_bits_revealed, n)
    _enc_send(dc, {"type": "DONE", "sacrificed": sacrificed})

    return ReconcileResult(
        reconciled_bits=bits[sacrificed:],
        bits_corrected=0,    # Alice corrects nothing; she is the reference
        bits_sacrificed=sacrificed,
    )


# ---------------------------------------------------------------------------
# Core Reconciliation - Bob (Corrected) Side
# ---------------------------------------------------------------------------


def reconcile_bob(
    raw_bits: list[int],
    qber: float,
    dc,
) -> ReconcileResult:
    r"""
    Run single-pass reconciliation - Bob (corrected) side.

    Receives Alice's block parities, identifies mismatches, participates in
    binary search for each, applies corrections, and trims the same number of
    bits as Alice for privacy amplification.

    ----------
    Parameters
    ----------
    raw_bits : list[int]
        Bob's raw sifted key bits (after check-bit split, before HKDF).
    qber : float
        QBER measured during the check-bit phase (used for block sizing).
    dc : DataChannel
        Classical data channel - used BEFORE the payload transfer begins.

    -------
    Returns
    -------
    ReconcileResult
        reconciled_bits ready for derive_key().
        bits_corrected = number of bit flips applied.
        bits_sacrificed = same as Alice (must match exactly).
    """
    bits = list(raw_bits)

    # Step 1: receive block parities
    msg = _dec_recv(dc)
    assert msg["type"] == "PARITIES", f"Expected PARITIES, got {msg['type']}"
    alice_parities: list[int] = msg["parities"]
    bsz: int = msg["bsz"]
    n: int = msg["n"]

    if n == 0:
        _enc_send(dc, {"mismatch_indices": []})
        _dec_recv(dc)   # DONE
        return ReconcileResult(reconciled_bits=[], bits_corrected=0, bits_sacrificed=0)

    blocks = _make_blocks(n, bsz)

    # Identify blocks where Bob's parity differs from Alice's
    mismatch_indices = [
        bidx for bidx, (s, e) in enumerate(blocks)
        if _parity(bits, s, e) != alice_parities[bidx]
    ]

    # Step 2: send mismatch indices
    _enc_send(dc, {"mismatch_indices": mismatch_indices})

    bits_corrected = 0

    # Step 3: binary search + correction
    for bidx in mismatch_indices:
        s, e = blocks[bidx]
        lo, hi = s, e
        max_depth = math.ceil(math.log2(max(e - s, 2))) + 2  # hard cap
        depth = 0

        while hi - lo > 1:
            depth += 1
            if depth > max_depth:
                raise RuntimeError(
                    f"Bisect depth {depth} exceeded cap {max_depth}: "
                    f"block={bidx} lo={lo} hi={hi} bsz={bsz} - logic error"
                )
            mid = (lo + hi) // 2
            msg = _dec_recv(dc)
            assert msg["type"] == "SUBPARITY"
            alice_sp = msg["sp"]
            bob_sp = _parity(bits, lo, mid)
            if bob_sp != alice_sp:
                # Error is in the left half [lo, mid)
                _enc_send(dc, {"go": "L"})
                hi = mid
            else:
                # Error is in the right half [mid, hi)
                _enc_send(dc, {"go": "R"})
                lo = mid

        # Receive Alice's authoritative bit value and apply correction
        msg = _dec_recv(dc)
        assert msg["type"] == "CORRECT"
        pos, val = msg["pos"], msg["val"]
        if bits[pos] != val:
            bits[pos] = val
            bits_corrected += 1

    # Step 4: receive sacrificed count and trim (must match Alice exactly)
    msg = _dec_recv(dc)
    assert msg["type"] == "DONE"
    sacrificed: int = msg["sacrificed"]

    return ReconcileResult(
        reconciled_bits=bits[sacrificed:],
        bits_corrected=bits_corrected,
        bits_sacrificed=sacrificed,
    )


# ---------------------------------------------------------------------------
# Post-Reconciliation Key Verification
# ---------------------------------------------------------------------------


_VERIFY_FAIL = b"hbd-verify-fail"
_VERIFY_PING = b"hbd-verify-ping"
_VERIFY_PONG = b"hbd-verify-pong"


def verify_key_alice(dc, aes_key: bytes) -> None:
    r"""
    Send a short AES-GCM-authenticated ping; receive and verify Bob's pong.

    Failure path (keys differ or any exception): Bob sends an explicit failure
    marker encrypted with the constant _RECON_AUTH_KEY. Alice receives it,
    tries to decrypt with aes_key first (succeeds on the happy path), falls
    through to _RECON_AUTH_KEY on mismatch. This guarantees Alice's recv()
    always unblocks regardless of what exception Bob raised.

    Robustness: if Alice's own ping send or pong receive raises for any reason,
    Alice sends a failure marker to unblock Bob before propagating the exception.
    """
    try:
        dc.send(encrypt(_VERIFY_PING, aes_key))
        raw = dc.recv()
    except Exception as exc:
        # Something broke before we could exchange messages.
        # Try to unblock Bob's recv() with a failure marker; ignore send errors.
        try:
            dc.send(encrypt(_VERIFY_FAIL, _RECON_AUTH_KEY))
        except Exception:
            try:
                dc.close()
            except Exception:
                pass
        raise ReconciliationIncompleteError(
            f"Key verification failed during exchange: {exc}"
        ) from exc

    # Try to decode as a valid pong (success path: keys match)
    try:
        pong = decrypt(raw, aes_key)
        if pong == _VERIFY_PONG:
            return  # keys match - all good
        raise ReconciliationIncompleteError(
            f"Key verification: unexpected pong value {pong!r}"
        )
    except InvalidTag:
        pass  # fall through: Bob may have sent the failure marker instead

    # Try to decode as Bob's explicit failure marker (keys-differ path)
    try:
        marker = decrypt(raw, _RECON_AUTH_KEY)
        if marker == _VERIFY_FAIL:
            raise ReconciliationIncompleteError(
                "Key verification failed: Bob reported key mismatch. "
                "Likely cause: even-count errors in a block (single-pass limitation)."
            )
    except InvalidTag:
        pass

    # Neither pong nor failure marker decoded - treat as mismatch
    raise ReconciliationIncompleteError(
        "Key verification failed: unrecognised response from Bob."
    )


def verify_key_bob(dc, aes_key: bytes) -> None:
    r"""
    Receive Alice's ping and reply with an authenticated pong.

    Robustness: if ANY exception occurs - decrypt failure (InvalidTag), wrong
    plaintext, socket error, or anything else - Bob sends a failure marker
    encrypted with the constant _RECON_AUTH_KEY before raising, so Alice's
    recv() always unblocks. If even the failure-marker send fails, Bob
    closes the socket explicitly to break Alice's recv().
    """
    try:
        ping = decrypt(dc.recv(), aes_key)
        if ping != _VERIFY_PING:
            raise ReconciliationIncompleteError(
                f"Key verification: unexpected ping value {ping!r}"
            )
        dc.send(encrypt(_VERIFY_PONG, aes_key))
    except Exception as exc:
        # No matter what went wrong, unblock Alice's recv() before propagating.
        try:
            dc.send(encrypt(_VERIFY_FAIL, _RECON_AUTH_KEY))
        except Exception:
            try:
                dc.close()
            except Exception:
                pass
        if isinstance(exc, ReconciliationIncompleteError):
            raise
        raise ReconciliationIncompleteError(
            "Key verification failed: AES-GCM tag mismatch after reconciliation. "
            "Likely cause: even-count errors in a block (single-pass limitation)."
        ) from exc


# ---------------------------------------------------------------------------
# Private Helpers
# ---------------------------------------------------------------------------


def _make_blocks(n: int, bsz: int) -> list[tuple[int, int]]:
    r"""Partition [0, n) into consecutive blocks of size bsz (last may be shorter)."""
    blocks: list[tuple[int, int]] = []
    i = 0
    while i < n:
        blocks.append((i, min(i + bsz, n)))
        i += bsz
    return blocks


def _parity(bits: list[int], lo: int, hi: int) -> int:
    r"""XOR parity of bits[lo:hi]."""
    return sum(bits[lo:hi]) % 2


def _enc_send(dc, msg: dict) -> None:
    r"""Encrypt and send a JSON message using the constant reconciliation auth key."""
    dc.send(encrypt(json.dumps(msg).encode(), _RECON_AUTH_KEY))


def _dec_recv(dc) -> dict:
    r"""Receive and decrypt a JSON message from the reconciliation channel."""
    return json.loads(decrypt(dc.recv(), _RECON_AUTH_KEY).decode())