"""
Phase 6 — Echo validation + adaptive rerouting tests.

Structure
---------
1. Unit tests — _find_mismatch_ranges, _classify_source
2. Echo protocol — clean pass (isolated, using socketpairs)
3. Echo protocol — mismatch + QBER above ANOMALY_THRESHOLD
     → classified as QUANTUM_CHANNEL_ISSUE, logged at WARNING
     → rerouted (only mismatched range), recovered, logged at INFO
4. Echo protocol — mismatch + QBER at/below ANOMALY_THRESHOLD
     → classified as POSSIBLE_EAVESDROPPER
     → security event logged at CRITICAL via "echo_validation.SECURITY"
     → rerouted and recovered
5. Echo protocol — persistent mismatch (buggy re-echo from Bob)
     → ChannelFailureError raised after one retry (no infinite loop)
6. Retransmit size check — only mismatched byte range sent, not full payload
7. Full integration test — QKD + split + alice_transfer + bob_transfer,
     noiseless conditions, payload matches at both ends

Logging verification strategy
------------------------------
`get_logger()` sets propagate=False on all loggers, so pytest's caplog
(which attaches a handler to the root logger) cannot capture these logs.
Instead we attach a _CaptureHandler directly to the target logger, run
the protocol, and inspect captured records.  The handler is always removed
in the finally block to prevent cross-test bleed.
"""

import json
import logging
import socket
import threading

import pytest

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from classical.aes_channel import derive_key, encrypt, decrypt
from classical.transport import send_frame, recv_frame
from quantum.bb84 import run_bb84
from server.ebit_server import EbitServer
from node.node import Node
from split_controller.controller import compute_split
from transmission.echo_validation import (
    ANOMALY_THRESHOLD,
    EchoResult,
    ChannelFailureError,
    alice_transfer,
    bob_transfer,
    _alice_echo_phase,
    _bob_echo_phase,
    _find_mismatch_ranges,
    _classify_source,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _qkd_key() -> bytes:
    result = run_bb84(n_qubits=200, injected_noise=0.0, seed=42)
    return derive_key(result.sifted_key)


class _SimpleDC:
    """Minimal DataChannel-compatible wrapper around a raw socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._conn = sock

    def send(self, data: bytes) -> None:
        send_frame(self._conn, data)

    def recv(self) -> bytes:
        return recv_frame(self._conn)

    def close(self) -> None:
        self._conn.close()


class _CaptureHandler(logging.Handler):
    """Logging handler that collects records for inspection in tests."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _run_echo_protocol(
    original: bytes,
    bob_received: bytes,
    quantum_len: int,
    aes_key: bytes,
    qber: float,
) -> EchoResult:
    """
    Run the echo protocol with socketpairs:
      Alice holds `original`, Bob holds `bob_received` (may differ).
    Returns EchoResult or re-raises ChannelFailureError.
    """
    sock_a, sock_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    dc_a = _SimpleDC(sock_a)
    dc_b = _SimpleDC(sock_b)
    result: list[EchoResult | None] = [None]
    bob_error: list[Exception | None] = [None]

    def run_bob() -> None:
        try:
            _bob_echo_phase(bob_received, dc_b, aes_key)
        except Exception as exc:
            bob_error[0] = exc

    t = threading.Thread(target=run_bob, daemon=True)
    t.start()
    try:
        result[0] = _alice_echo_phase(original, quantum_len, dc_a, aes_key, qber)
    finally:
        t.join(timeout=10)
        sock_a.close()
        sock_b.close()

    if bob_error[0] is not None:
        raise bob_error[0]
    return result[0]


def _run_persistent_mismatch(
    original: bytes,
    bob_received: bytes,
    quantum_len: int,
    aes_key: bytes,
    qber: float,
) -> None:
    """
    Simulate a channel that corrupts data even after the retransmit:
    Bob echoes the corrupted payload, receives Alice's PATCH, but sends
    back garbage instead of the corrected range.
    """
    sock_a, sock_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    dc_a = _SimpleDC(sock_a)
    dc_b = _SimpleDC(sock_b)

    def buggy_bob() -> None:
        # Step 1: send echo of (corrupted) received payload
        dc_b.send(encrypt(bob_received, aes_key))
        # Step 2: receive Alice's PATCH frame (discard — simulate persistent corruption)
        dc_b.recv()
        # Step 3: send wrong re-echo instead of the patched range
        dc_b.send(encrypt(b"\x00\x00\x00\x00\x00", aes_key))

    t = threading.Thread(target=buggy_bob, daemon=True)
    t.start()
    try:
        _alice_echo_phase(original, quantum_len, dc_a, aes_key, qber)
    finally:
        t.join(timeout=10)
        sock_a.close()
        sock_b.close()


# ---------------------------------------------------------------------------
# 1. Unit tests — helpers
# ---------------------------------------------------------------------------

class TestFindMismatchRanges:
    def test_identical_bytes(self):
        assert _find_mismatch_ranges(b"abc", b"abc") == []

    def test_single_byte_diff(self):
        assert _find_mismatch_ranges(b"abc", b"axc") == [(1, 2)]

    def test_all_different(self):
        assert _find_mismatch_ranges(b"aaa", b"bbb") == [(0, 3)]

    def test_contiguous_run(self):
        a = b"AAABBBCCC"
        b_ = b"AAAXYZCCC"
        assert _find_mismatch_ranges(a, b_) == [(3, 6)]

    def test_two_disjoint_ranges(self):
        a  = b"XAAAXAAA"
        b_ = b"XAAAYAAB"
        ranges = _find_mismatch_ranges(a, b_)
        assert (4, 5) in ranges
        assert (7, 8) in ranges

    def test_empty_inputs(self):
        assert _find_mismatch_ranges(b"", b"") == []

    def test_different_lengths_longer_b(self):
        ranges = _find_mismatch_ranges(b"ab", b"abc")
        # The extra byte in b is a "mismatch" (a has nothing there)
        assert any(s >= 2 for s, e in ranges)


class TestClassifySource:
    def test_purely_quantum(self):
        # quantum_len=5; mismatch at [2,4] — entirely in [0,5)
        assert _classify_source([(2, 4)], quantum_len=5, total_len=10) == "quantum"

    def test_purely_classical(self):
        # quantum_len=5; mismatch at [6,8] — entirely in [5,10)
        assert _classify_source([(6, 8)], quantum_len=5, total_len=10) == "classical"

    def test_spans_both(self):
        # quantum_len=5; mismatch at [3,7] — crosses boundary
        assert _classify_source([(3, 7)], quantum_len=5, total_len=10) == "both"

    def test_boundary_at_start_of_classical(self):
        # range starts exactly at quantum_len — should be classical
        assert _classify_source([(5, 8)], quantum_len=5, total_len=10) == "classical"

    def test_no_quantum_segment(self):
        # quantum_len=0 (all-classical path)
        assert _classify_source([(0, 5)], quantum_len=0, total_len=10) == "classical"


# ---------------------------------------------------------------------------
# 2. Clean first-pass match
# ---------------------------------------------------------------------------

def test_echo_clean_pass():
    """Noiseless conditions: echo matches, no rerouting triggered."""
    aes_key = _qkd_key()
    payload = b"clean-pass-test-payload!"
    quantum_len = 12  # arbitrary — no mismatch so boundary doesn't matter

    result = _run_echo_protocol(
        original=payload,
        bob_received=payload,   # identical — no corruption
        quantum_len=quantum_len,
        aes_key=aes_key,
        qber=0.0,
    )

    assert result.success is True
    assert result.recovered is False
    assert result.mismatched_ranges == []
    assert result.diagnosis is None
    assert result.retransmit_bytes == 0
    print(f"\n[clean_pass] success={result.success}, recovered={result.recovered}")


# ---------------------------------------------------------------------------
# 3. Mismatch with elevated QBER → QUANTUM_CHANNEL_ISSUE
# ---------------------------------------------------------------------------

def test_echo_quantum_channel_issue():
    """
    Mismatch in quantum segment + QBER above ANOMALY_THRESHOLD.
    Expected:
      - diagnosis == QUANTUM_CHANNEL_ISSUE
      - WARNING logged from "echo_validation"
      - no CRITICAL from "echo_validation.SECURITY"
      - rerouted and recovered (recovered=True)
      - only the mismatched bytes were retransmitted
    """
    aes_key = _qkd_key()
    payload      = bytes(range(20))          # 20 bytes, values 0–19
    quantum_len  = 10                        # bytes [0,10) = quantum, [10,20) = classical
    # Corrupt bytes 3–5 (quantum segment)
    corrupted    = bytearray(payload)
    corrupted[3] = 0xFF
    corrupted[4] = 0xFF
    corrupted    = bytes(corrupted)

    # QBER above ANOMALY_THRESHOLD (0.05) but below session-abort (0.11)
    high_qber = ANOMALY_THRESHOLD + 0.02    # 0.07

    warn_handler = _CaptureHandler()
    sec_handler  = _CaptureHandler()
    echo_logger  = logging.getLogger("echo_validation")
    sec_logger   = logging.getLogger("echo_validation.SECURITY")
    echo_logger.addHandler(warn_handler)
    sec_logger.addHandler(sec_handler)
    try:
        result = _run_echo_protocol(
            original=payload,
            bob_received=corrupted,
            quantum_len=quantum_len,
            aes_key=aes_key,
            qber=high_qber,
        )
    finally:
        echo_logger.removeHandler(warn_handler)
        sec_logger.removeHandler(sec_handler)

    assert result.success is True
    assert result.recovered is True
    assert result.diagnosis == "QUANTUM_CHANNEL_ISSUE"
    assert result.mismatch_source == "quantum"
    # Only bytes 3–4 were retransmitted (2 bytes), not the full 20
    assert result.retransmit_bytes == 2
    assert result.retransmit_bytes < len(payload)

    # A WARNING was logged from the main echo_validation logger
    warn_records = [r for r in warn_handler.records if r.levelno == logging.WARNING]
    assert warn_records, "Expected a WARNING log record for QUANTUM_CHANNEL_ISSUE"
    assert "QUANTUM_CHANNEL_ISSUE" in warn_records[0].getMessage() or \
           warn_records[0].__dict__.get("diagnosis") == "QUANTUM_CHANNEL_ISSUE"

    # No CRITICAL security event should have been raised
    assert not sec_handler.records, "Should NOT log a security CRITICAL for QUANTUM_CHANNEL_ISSUE"

    print(f"\n[quantum_issue] diagnosis={result.diagnosis}  "
          f"source={result.mismatch_source}  retransmit={result.retransmit_bytes}B  "
          f"warn_count={len(warn_records)}")


# ---------------------------------------------------------------------------
# 4. Mismatch with low QBER → POSSIBLE_EAVESDROPPER
# ---------------------------------------------------------------------------

def test_echo_possible_eavesdropper():
    """
    Mismatch in quantum segment + QBER at/below ANOMALY_THRESHOLD.
    Expected:
      - diagnosis == POSSIBLE_EAVESDROPPER
      - CRITICAL logged from "echo_validation.SECURITY" (distinct logger + level)
      - no WARNING from "echo_validation" main logger
      - rerouted and recovered (recovered=True)
    """
    aes_key = _qkd_key()
    payload      = bytes(range(20))
    quantum_len  = 10
    # Corrupt bytes 1–3 (quantum segment)
    corrupted    = bytearray(payload)
    corrupted[1] = 0xAB
    corrupted[2] = 0xCD
    corrupted    = bytes(corrupted)

    # QBER low/normal — suspicious given the mismatch
    low_qber = ANOMALY_THRESHOLD - 0.02    # 0.03

    warn_handler = _CaptureHandler()
    sec_handler  = _CaptureHandler()
    echo_logger  = logging.getLogger("echo_validation")
    sec_logger   = logging.getLogger("echo_validation.SECURITY")
    echo_logger.addHandler(warn_handler)
    sec_logger.addHandler(sec_handler)
    try:
        result = _run_echo_protocol(
            original=payload,
            bob_received=corrupted,
            quantum_len=quantum_len,
            aes_key=aes_key,
            qber=low_qber,
        )
    finally:
        echo_logger.removeHandler(warn_handler)
        sec_logger.removeHandler(sec_handler)

    assert result.success is True
    assert result.recovered is True
    assert result.diagnosis == "POSSIBLE_EAVESDROPPER"
    assert result.mismatch_source == "quantum"
    assert result.retransmit_bytes < len(payload), "Must retransmit only the mismatch, not full payload"

    # CRITICAL must appear in the dedicated security logger
    crit_records = [r for r in sec_handler.records if r.levelno == logging.CRITICAL]
    assert crit_records, (
        "Expected a CRITICAL security log record from 'echo_validation.SECURITY' "
        "for POSSIBLE_EAVESDROPPER — it was missing"
    )
    crit_msg = crit_records[0].getMessage()
    assert "eavesdropper" in crit_msg.lower() or "SECURITY EVENT" in crit_msg, (
        f"CRITICAL record message does not look like a security event: {crit_msg!r}"
    )

    # Main logger should NOT have raised a WARNING for this path
    warn_records = [r for r in warn_handler.records if r.levelno == logging.WARNING]
    assert not warn_records, (
        "POSSIBLE_EAVESDROPPER should log to security logger (CRITICAL), "
        "not to main logger (WARNING)"
    )

    print(f"\n[eavesdropper] diagnosis={result.diagnosis}  "
          f"source={result.mismatch_source}  retransmit={result.retransmit_bytes}B")
    print(f"  CRITICAL security record: {crit_msg!r}")
    print(f"  Logger name: {crit_records[0].name!r}  Level: {crit_records[0].levelname!r}")


# ---------------------------------------------------------------------------
# 5. Persistent mismatch → ChannelFailureError
# ---------------------------------------------------------------------------

def test_echo_persistent_mismatch():
    """
    If the re-echo still mismatches after retransmit, raise ChannelFailureError.
    No infinite loop — exactly one retry before aborting.
    """
    aes_key     = _qkd_key()
    payload     = bytes(range(20))
    quantum_len = 10
    corrupted   = bytearray(payload)
    corrupted[5] = 0x00
    corrupted    = bytes(corrupted)

    with pytest.raises(ChannelFailureError) as exc_info:
        _run_persistent_mismatch(
            original=payload,
            bob_received=corrupted,
            quantum_len=quantum_len,
            aes_key=aes_key,
            qber=0.02,   # low QBER → eavesdropper diagnosis
        )

    err = exc_info.value
    assert err.ranges, "ChannelFailureError must carry the bad byte range(s)"
    assert err.diagnosis in ("QUANTUM_CHANNEL_ISSUE", "POSSIBLE_EAVESDROPPER")
    print(f"\n[persistent_mismatch] raised: {err}")


# ---------------------------------------------------------------------------
# 6. Retransmit size: only the mismatched range, not the full payload
# ---------------------------------------------------------------------------

def test_echo_only_mismatched_range_retransmitted():
    """
    Corrupt 4 bytes in a 50-byte payload; confirm retransmit_bytes == 4 << 50.

    This verifies the adaptive part of adaptive rerouting: we send only the
    affected bytes, not a full payload retransmit.
    """
    aes_key     = _qkd_key()
    payload     = b"A" * 50
    quantum_len = 25     # [0,25) quantum, [25,50) classical

    # Corrupt bytes 10–13 (quantum segment, 4 bytes)
    corrupted = bytearray(payload)
    for i in range(10, 14):
        corrupted[i] = 0x00
    corrupted = bytes(corrupted)

    result = _run_echo_protocol(
        original=payload,
        bob_received=corrupted,
        quantum_len=quantum_len,
        aes_key=aes_key,
        qber=ANOMALY_THRESHOLD + 0.01,   # quantum channel issue
    )

    assert result.success is True
    assert result.recovered is True
    assert result.retransmit_bytes == 4, (
        f"Expected 4 bytes retransmitted (the mismatch), got {result.retransmit_bytes}"
    )
    assert result.retransmit_bytes < len(payload), (
        "Full payload must NOT have been retransmitted"
    )
    print(f"\n[retransmit_size] payload={len(payload)}B  retransmit={result.retransmit_bytes}B  "
          f"ratio={result.retransmit_bytes/len(payload):.0%}")


# ---------------------------------------------------------------------------
# 7. Full integration — QKD + split + alice_transfer + bob_transfer
# ---------------------------------------------------------------------------

def _make_config(server_port: int, data_port: int, quantum_port: int) -> dict:
    return {
        "host": "127.0.0.1",
        "server_port": server_port,
        "data_port": data_port,
        "quantum_port": quantum_port,
        "protocol": "BB84",
        "injected_qber": 0.0,
        "n_qubits": 200,
        "n_pairs_per_setting": 300,
        "security_level": "medium",
        "payload_size_bytes": 12,
        "available_ebits": 50,
    }


def _run_full_transfer(config: dict, payload: bytes) -> tuple[EchoResult, bytes, dict]:
    """
    Full end-to-end pipeline: QKD → split → alice_transfer (Phase 5 + 6)
    on Alice's side; bob_transfer on Bob's side.

    Returns (echo_result, bob_final_payload, errors).
    """
    from classical.transport import DataChannel

    server = EbitServer(config)
    server.start()
    server.ready.wait(timeout=5)

    node_a = Node("A", config)
    node_b = Node("B", config)

    errors: dict[str, Exception] = {}
    b_registered     = threading.Event()
    b_channels_ready = threading.Event()
    bob_final: list[bytes | None] = [None]
    alice_result: list[EchoResult | None] = [None]

    def run_b() -> None:
        try:
            node_b.connect()
            node_b.register()
            b_registered.set()
            node_b.wait_for_connection()
            node_b.run_qkd()
            aes_key = derive_key(node_b.session.key)

            c_dc = DataChannel(config["host"], config["data_port"])
            q_dc = DataChannel(config["host"], config["quantum_port"])
            c_dc.listen()
            q_dc.listen()
            b_channels_ready.set()
            c_dc.accept()
            q_dc.accept()

            bob_final[0] = bob_transfer(node_b, c_dc, q_dc, aes_key)
            c_dc.close()
            q_dc.close()
        except Exception as exc:
            errors["B"] = exc
            b_registered.set()
            b_channels_ready.set()
        finally:
            node_b.close()

    def run_a() -> None:
        try:
            b_registered.wait(timeout=10)
            if "B" in errors:
                return

            node_a.connect()
            node_a.register()
            node_a.wait_for_peer("B", timeout=10)
            node_a.request_connection("B")
            node_a.run_qkd()
            aes_key = derive_key(node_a.session.key)

            b_channels_ready.wait(timeout=10)
            c_dc = DataChannel(config["host"], config["data_port"])
            q_dc = DataChannel(config["host"], config["quantum_port"])
            c_dc.connect()
            q_dc.connect()

            decision = compute_split(
                security_level=config["security_level"],
                payload_size_bytes=len(payload),
                available_ebits=config["available_ebits"],
                qber=node_a.session.qber,
            )
            alice_result[0] = alice_transfer(
                node_a, payload, decision, c_dc, q_dc, aes_key,
                config["available_ebits"], node_a.session.qber,
            )
            c_dc.close()
            q_dc.close()
        except Exception as exc:
            errors["A"] = exc
        finally:
            node_a.close()

    tb = threading.Thread(target=run_b, daemon=True)
    ta = threading.Thread(target=run_a, daemon=True)
    tb.start()
    ta.start()
    tb.join(timeout=120)
    ta.join(timeout=120)
    server.stop()

    return alice_result[0], bob_final[0], errors


def test_integration_clean_pass_noiseless():
    """
    Full pipeline: QKD → split → alice_transfer → bob_transfer.
    Noiseless conditions → clean first-pass echo match, no rerouting.
    Both Alice's EchoResult and Bob's final payload must match the original.
    """
    payload = b"hbd-test-p6!"   # 12 bytes; medium → 6 q bytes, 24 ebits ≤ 50
    config  = _make_config(_free_port(), _free_port(), _free_port())

    result, bob_payload, errors = _run_full_transfer(config, payload)

    assert not errors, f"Errors in threads: {errors}"
    assert result is not None, "alice_transfer returned None"
    assert result.success is True
    assert result.recovered is False,  "Noiseless run should NOT need rerouting"
    assert result.mismatched_ranges == []
    assert result.retransmit_bytes == 0

    assert bob_payload == payload, (
        f"Bob's final payload doesn't match!\n  sent   : {payload!r}\n  got    : {bob_payload!r}"
    )

    print(f"\n[integration] payload={payload!r}")
    print(f"  alice result: success={result.success}  recovered={result.recovered}  "
          f"retransmit={result.retransmit_bytes}B")
    print(f"  bob final   : {bob_payload!r}")
    print(f"  outcome     : CLEAN_PASS ✓")
