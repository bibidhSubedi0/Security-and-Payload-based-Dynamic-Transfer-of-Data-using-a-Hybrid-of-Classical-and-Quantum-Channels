"""
Phase 8 Part B — Eavesdropper simulation tests.

Validates the intercept-resend attack implementation against theoretical
expectations and confirms that the existing defence layers (Phase 2 QBER
abort, Phase 6 echo comparison) catch Eve's interference.

Test structure
--------------
1. QBER under Eve lands in expected statistical range [0.15, 0.35]
2. Eve's QBER reliably exceeds the 0.11 session-abort threshold
3. Full session with EveEbitServer → SessionAbortedError before payload
4. Eve never sees plaintext payload (confirmed by abort ordering)
5. Phase 6 echo is a complementary detection layer (not a replacement)

EveEbitServer design
--------------------
A minimal subclass of EbitServer that overrides _simulate_qkd() to use
run_bb84_with_eve() for the BB84 key-exchange step.  All other server
behaviour (connection handling, retries, registry, etc.) is unchanged.
This class lives here, in the test module, since it is a test-only artifact
and keeping it here avoids importing any server logic into the quantum module.
"""

import socket
import threading

import pytest

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from classical.aes_channel import derive_key
from classical.transport import DataChannel
from node.node import Node, SessionAbortedError
from server.ebit_server import EbitServer
from quantum.bb84 import BB84_QBER_THRESHOLD
from quantum.eavesdropper import EveInterceptResult, run_bb84_with_eve
from transmission.echo_validation import (
    EchoResult,
    _alice_echo_phase,
    _bob_echo_phase,
    ANOMALY_THRESHOLD,
)
from classical.transport import send_frame, recv_frame


# ---------------------------------------------------------------------------
# EveEbitServer — test-only subclass
# ---------------------------------------------------------------------------

class EveEbitServer(EbitServer):
    """
    EbitServer variant that substitutes run_bb84_with_eve() for the
    BB84 key-exchange step.  The session-abort retry loop (MAX_RETRIES=2)
    is inherited unchanged — Eve's QBER should trigger it consistently.
    """

    def _simulate_qkd(self, protocol, noise, n_qubits):
        """Override: use Eve's intercept-resend instead of honest BB84."""
        result = run_bb84_with_eve(n_qubits=n_qubits)

        # Extract alice_key / bob_key exactly as the honest server does
        # (positions where alice_basis == bob_basis, second half)
        matching = [
            i for i in range(n_qubits)
            if result.alice_bases[i] == result.bob_bases[i]
        ]
        split = max(1, len(matching) // 2)
        key_positions = matching[split:]
        alice_key = [result.alice_bits[i] for i in key_positions]
        bob_key   = [result.bob_results[i] for i in key_positions]

        qber   = result.qber
        chsh   = None
        secure = qber <= BB84_QBER_THRESHOLD

        return alice_key, bob_key, qber, chsh, secure


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _qkd_key() -> bytes:
    from quantum.bb84 import run_bb84
    r = run_bb84(n_qubits=200, injected_noise=0.0, seed=42)
    return derive_key(r.sifted_key)


class _SimpleDC:
    def __init__(self, sock):
        self._conn = sock
    def send(self, data):
        send_frame(self._conn, data)
    def recv(self):
        return recv_frame(self._conn)
    def close(self):
        self._conn.close()


def _make_config(server_port: int, n_qubits: int = 400) -> dict:
    return {
        "host": "127.0.0.1",
        "server_port": server_port,
        "data_port": _free_port(),
        "quantum_port": _free_port(),
        "protocol": "BB84",
        "injected_qber": 0.0,
        "n_qubits": n_qubits,    # 400 → P(QBER<0.11 | Eve) ≈ 0.06%
        "security_level": "medium",
        "available_ebits": 100,
    }


# ---------------------------------------------------------------------------
# Test 1 — QBER under Eve: statistical range check
# ---------------------------------------------------------------------------

def test_eve_qber_in_expected_range():
    """
    Intercept-resend should produce QBER ≈ 0.25 (25%).

    Tolerance: [0.15, 0.35] — roughly ±2σ for n_qubits=400
    (σ ≈ √(0.25×0.75/100) ≈ 4.3% on ~100 check bits).

    We fix a seed for reproducibility, but the range is wide enough that
    any seed should pass for n_qubits≥200.
    """
    result = run_bb84_with_eve(n_qubits=400, seed=7)

    print(f"\n[eve_qber] QBER={result.qber:.4f}  "
          f"check_bits={result.qber_sample_size}  "
          f"expected≈0.25")
    print(f"  alice_bits sample: {result.alice_bits[:10]}")
    print(f"  eve_bases  sample: {result.eve_bases[:10]}")
    print(f"  eve_results sample:{result.eve_results[:10]}")
    print(f"  bob_results sample:{result.bob_results[:10]}")

    assert 0.15 <= result.qber <= 0.35, (
        f"QBER under intercept-resend was {result.qber:.4f}, "
        f"expected in [0.15, 0.35]. "
        f"(check_sample={result.qber_sample_size})"
    )


# ---------------------------------------------------------------------------
# Test 2 — QBER under Eve exceeds the 0.11 abort threshold
# ---------------------------------------------------------------------------

def test_eve_qber_exceeds_abort_threshold():
    """
    Eve's ~25% QBER must reliably exceed BB84_QBER_THRESHOLD (0.11).
    Test with multiple independent seeds for statistical confidence.
    """
    n_trials   = 5
    n_qubits   = 300   # sufficient sifted-key length for reliable signal
    above_threshold = 0

    for seed in range(n_trials):
        result = run_bb84_with_eve(n_qubits=n_qubits, seed=seed * 17)
        if result.qber > BB84_QBER_THRESHOLD:
            above_threshold += 1
        print(f"  seed={seed*17}  QBER={result.qber:.4f}  "
              f"exceeds_threshold={result.qber > BB84_QBER_THRESHOLD}")

    # All 5 trials should exceed threshold (P(QBER < 0.11 | Eve) ≈ 1%)
    assert above_threshold == n_trials, (
        f"Only {above_threshold}/{n_trials} trials had QBER > 0.11. "
        f"Expected all {n_trials} (Eve's intercept-resend should consistently exceed threshold)."
    )
    print(f"\n[eve_threshold] {above_threshold}/{n_trials} trials exceeded 0.11 ✓")


# ---------------------------------------------------------------------------
# Test 3 — Full transfer attempt with Eve → SessionAbortedError
# ---------------------------------------------------------------------------

def test_eve_causes_session_abort():
    """
    With EveEbitServer, run the full pipeline.  The server attempts QKD
    with Eve's QBER (~25%) and should abort after MAX_RETRIES=2 attempts.
    Both nodes must receive SessionAbortedError; no payload is transmitted.
    """
    config = _make_config(_free_port())
    server = EveEbitServer(config)
    server.start()
    server.ready.wait(timeout=5)

    node_a = Node("A", config)
    node_b = Node("B", config)

    errors: dict = {}
    b_registered = threading.Event()
    a_exception:  list = [None]
    b_exception:  list = [None]
    payload_transmitted = threading.Event()   # must NEVER be set

    def run_b():
        try:
            node_b.connect()
            node_b.register()
            b_registered.set()
            node_b.wait_for_connection()
            node_b.run_qkd()   # should raise SessionAbortedError
        except SessionAbortedError as exc:
            b_exception[0] = exc
        except Exception as exc:
            errors["B"] = exc
        finally:
            node_b.close()

    def run_a():
        try:
            b_registered.wait(timeout=10)
            node_a.connect()
            node_a.register()
            node_a.wait_for_peer("B", timeout=10)
            node_a.request_connection("B")
            node_a.run_qkd()   # should raise SessionAbortedError
            # If we reach here without exception, Eve wasn't caught — FAIL
            payload_transmitted.set()  # this line must not execute
        except SessionAbortedError as exc:
            a_exception[0] = exc
        except Exception as exc:
            errors["A"] = exc
        finally:
            node_a.close()

    tb = threading.Thread(target=run_b, daemon=True)
    ta = threading.Thread(target=run_a, daemon=True)
    tb.start(); ta.start()
    tb.join(timeout=60); ta.join(timeout=60)
    server.stop()

    assert not errors, f"Unexpected errors: {errors}"

    # Both nodes must have received a SessionAbortedError
    assert a_exception[0] is not None, (
        "Node A did NOT raise SessionAbortedError — Eve's QBER was not caught"
    )
    assert b_exception[0] is not None, (
        "Node B did NOT raise SessionAbortedError — Eve's QBER was not caught"
    )

    # The abort must name QBER as the reason
    assert a_exception[0].qber > BB84_QBER_THRESHOLD, (
        f"Session aborted but QBER {a_exception[0].qber:.4f} ≤ threshold 0.11 — "
        f"wrong abort reason"
    )

    print(f"\n[eve_abort] SessionAbortedError raised on both nodes ✓")
    print(f"  Node A abort reason: {a_exception[0].reason}")
    print(f"  Node A abort QBER  : {a_exception[0].qber:.4f}")
    print(f"  Node B abort QBER  : {b_exception[0].qber:.4f}")


# ---------------------------------------------------------------------------
# Test 4 — Eve never sees plaintext payload
# ---------------------------------------------------------------------------

def test_eve_never_sees_plaintext_payload():
    """
    Eve's interception terminates the session BEFORE any payload is sent.
    Confirm: SessionAbortedError is raised at the QKD stage, so
    transmit_payload / alice_transfer are NEVER called.

    We verify this by confirming:
      (a) SessionAbortedError is raised
      (b) node_a.last_received_payload is None (never set by receive_payload)
      (c) a sentinel Event that would be set by transmit_payload is NOT set

    In the real threat model, Eve sits on the quantum channel (the qubit
    transmission path) and can only interfere with key exchange.  AES-GCM
    with the QKD-derived key protects the payload — but more fundamentally,
    if QBER exceeds threshold, the abort happens before HKDF or any
    payload encryption even begins.
    """
    config = _make_config(_free_port())
    server = EveEbitServer(config)
    server.start()
    server.ready.wait(timeout=5)

    node_a = Node("A", config)
    node_b = Node("B", config)

    b_registered   = threading.Event()
    session_aborted = threading.Event()
    payload_attempt = threading.Event()   # set if transmit_payload is ever called

    def run_b():
        try:
            node_b.connect(); node_b.register()
            b_registered.set()
            node_b.wait_for_connection()
            node_b.run_qkd()
        except SessionAbortedError:
            session_aborted.set()
        finally:
            node_b.close()

    def run_a():
        try:
            b_registered.wait(timeout=10)
            node_a.connect(); node_a.register()
            node_a.wait_for_peer("B", timeout=10)
            node_a.request_connection("B")
            node_a.run_qkd()
            # If we reach this point without exception, Eve wasn't detected.
            # Any payload transmission from here would expose plaintext to Eve.
            payload_attempt.set()   # must NOT be set
        except SessionAbortedError:
            session_aborted.set()
        finally:
            node_a.close()

    tb = threading.Thread(target=run_b, daemon=True)
    ta = threading.Thread(target=run_a, daemon=True)
    tb.start(); ta.start()
    tb.join(timeout=60); ta.join(timeout=60)
    server.stop()

    assert session_aborted.is_set(), "Session was not aborted — Eve was not detected"
    assert not payload_attempt.is_set(), (
        "transmit_payload was called AFTER Eve interception — plaintext exposed!"
    )
    # No payload was ever set on node_b (receive_payload never ran)
    assert node_b.last_received_payload is None, (
        f"node_b.last_received_payload was set to {node_b.last_received_payload!r} "
        f"— receive_payload ran after Eve detection!"
    )

    print(f"\n[eve_no_payload] Session aborted before payload transmission ✓")
    print(f"  session_aborted={session_aborted.is_set()}")
    print(f"  payload_attempt={payload_attempt.is_set()} (expected False)")
    print(f"  node_b.last_received_payload={node_b.last_received_payload!r} (expected None)")


# ---------------------------------------------------------------------------
# Test 5 — Phase 6 echo as complementary detection layer
# ---------------------------------------------------------------------------

def test_echo_detects_eve_induced_mismatch():
    """
    If Eve's QBER happened to fall just under threshold on a given run
    (probabilistic — ~1% chance), Phase 6's echo comparison would still
    catch the resulting byte-level mismatch.

    We simulate this by:
      1. Running the honest pipeline (no Eve in QKD)
      2. Manually injecting a mismatch in the quantum segment of Bob's
         received payload (simulating the residual bit errors Eve introduces)
      3. Running the echo protocol
      4. Confirming Phase 6 detects and reroutes the mismatch

    This confirms the two detection layers are complementary:
      Layer 1 (Phase 2): QBER > 0.11 → abort (primary defence, catches ~99%)
      Layer 2 (Phase 6): echo mismatch → reroute (secondary, catches residual)
    """
    aes_key     = _qkd_key()
    original    = bytes(range(30))    # 30-byte payload
    quantum_len = 15                  # bytes [0,15) = quantum segment

    # Simulate Eve's residual errors in the quantum segment
    # (QBER just under threshold → a few bits still wrong)
    corrupted = bytearray(original)
    corrupted[3]  = original[3]  ^ 0xFF   # flip all bits at byte 3
    corrupted[7]  = original[7]  ^ 0x0F   # flip lower nibble at byte 7
    corrupted[12] = original[12] ^ 0xAA   # alternating bits at byte 12
    corrupted = bytes(corrupted)

    # QBER slightly above ANOMALY_THRESHOLD but below abort threshold
    simulated_qber = 0.08   # Eve "nearly undetected" in this run

    sock_a, sock_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    dc_a = _SimpleDC(sock_a)
    dc_b = _SimpleDC(sock_b)
    echo_result_h: list = [None]

    def run_bob():
        # Bob holds the corrupted version (Eve's residual errors)
        _bob_echo_phase(corrupted, dc_b, aes_key)

    t = threading.Thread(target=run_bob, daemon=True)
    t.start()

    echo_result = _alice_echo_phase(
        original, quantum_len, dc_a, aes_key, simulated_qber
    )
    t.join(timeout=10)
    sock_a.close(); sock_b.close()

    # Phase 6 must detect and recover
    assert echo_result.success is True,    "Phase 6 should recover from Eve's residual mismatch"
    assert echo_result.recovered is True,  "Recovery must be via reroute"
    assert echo_result.mismatch_source == "quantum", (
        "Mismatch should be in the quantum segment (Eve attacks qubit channel)"
    )
    assert echo_result.diagnosis == "QUANTUM_CHANNEL_ISSUE", (
        "QBER=0.08 > ANOMALY_THRESHOLD=0.05 → QUANTUM_CHANNEL_ISSUE"
    )
    assert echo_result.retransmit_bytes < len(original), (
        "Only the mismatched range should be retransmitted"
    )

    print(f"\n[echo_complementary] Phase 6 caught Eve's residual mismatch ✓")
    print(f"  mismatch_source={echo_result.mismatch_source}")
    print(f"  diagnosis={echo_result.diagnosis}")
    print(f"  retransmit_bytes={echo_result.retransmit_bytes} / {len(original)} total")
    print(f"  Layer 1 (Phase 2 QBER abort): primary defence   — catches ~99% of Eve attempts")
    print(f"  Layer 2 (Phase 6 echo):       secondary defence — catches residual mismatches")
