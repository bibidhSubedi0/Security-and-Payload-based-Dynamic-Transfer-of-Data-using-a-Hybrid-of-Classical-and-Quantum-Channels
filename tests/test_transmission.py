"""
Phase 5 — Dual-channel transmission tests.

Structure
---------
1. Payload splitter unit tests   — split / reassemble with various fractions
2. Quantum transmit unit tests   — SDC encode/decode bytes; EbitCapacityError
3. Integration test              — full pipeline: QKD → split → dual-channel
                                   concurrent tx → reassemble → byte-for-byte match
4. Concurrency check             — both channels overlap in time (not sequential)
5. Failure test                  — quantum segment too large → EbitCapacityError

Integration config note
-----------------------
payload = b"hbd-test-p5!" (12 bytes), medium security (quantum_fraction=0.5):
  quantum_len  = int(0.5 * 12) = 6 bytes  → ebits_needed = 6 * 4 = 24 ≤ 50 ✓
  classical_len = 6 bytes
available_ebits = 50 (from config, separate from the QKD key — they represent the
SDC ebit pool distributed by the server; the QKD key bits are used only for AES-256).
"""

import json
import socket
import threading
import time

import pytest

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from classical.aes_channel import derive_key
from classical.transport import DataChannel
from quantum.bb84 import run_bb84
from server.ebit_server import EbitServer
from node.node import Node
from split_controller.controller import (
    compute_split,
    QUANTUM_FRACTION_LOW,
    QUANTUM_FRACTION_MEDIUM,
    MAX_QUANTUM_FRACTION,
)
from transmission.payload_splitter import (
    split_payload,
    make_metadata,
    reassemble_from_segments,
    ReassemblyError,
    SplitPayload,
)
from transmission.quantum_transmit import (
    encode_bytes_sdc,
    ebits_needed,
    EbitCapacityError,
)
from transmission.classical_transmit import send_classical_segment, recv_classical_segment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


def _fake_decision(quantum_fraction: float):
    from split_controller.controller import SplitDecision
    return SplitDecision(
        quantum_fraction=quantum_fraction,
        classical_fraction=1.0 - quantum_fraction,
        reason="TEST",
    )


def _qkd_key() -> bytes:
    """Derive an AES key from a local BB84 run (no network)."""
    result = run_bb84(n_qubits=200, injected_noise=0.0, seed=42)
    return derive_key(result.sifted_key)


# ---------------------------------------------------------------------------
# 1. Payload splitter unit tests
# ---------------------------------------------------------------------------

PAYLOAD = b"Hello, hybrid quantum-classical system!"  # 39 bytes


@pytest.mark.parametrize("qf", [0.0, 0.25, 0.50, 0.75,
                                  QUANTUM_FRACTION_LOW,
                                  QUANTUM_FRACTION_MEDIUM,
                                  MAX_QUANTUM_FRACTION])
def test_splitter_round_trip(qf: float):
    decision = _fake_decision(qf)
    split = split_payload(PAYLOAD, decision)
    meta  = make_metadata(split)
    reconstructed = reassemble_from_segments(
        split.quantum_segment, split.classical_segment, meta
    )
    assert reconstructed == PAYLOAD, (
        f"qf={qf}: reconstructed != original\n"
        f"  original={PAYLOAD}\n  got={reconstructed}"
    )


def test_splitter_zero_quantum():
    split = split_payload(PAYLOAD, _fake_decision(0.0))
    assert split.quantum_segment == b""
    assert split.classical_segment == PAYLOAD
    assert split.quantum_len == 0
    assert split.total_len == len(PAYLOAD)


def test_splitter_full_quantum():
    split = split_payload(PAYLOAD, _fake_decision(1.0))
    assert split.quantum_segment == PAYLOAD
    assert split.classical_segment == b""
    assert split.classical_len == 0


def test_splitter_byte_boundary_is_floored():
    """int(0.5 * 39) = 19, not 20 — verify floor semantics."""
    split = split_payload(PAYLOAD, _fake_decision(0.5))
    expected_q = int(0.5 * len(PAYLOAD))
    assert split.quantum_len == expected_q
    assert split.classical_len == len(PAYLOAD) - expected_q


def test_splitter_segments_are_head_then_tail():
    """Quantum = HEAD bytes, classical = TAIL bytes — ordering Phase 6 relies on."""
    split = split_payload(PAYLOAD, _fake_decision(0.25))
    qlen = int(0.25 * len(PAYLOAD))
    assert split.quantum_segment  == PAYLOAD[:qlen]
    assert split.classical_segment == PAYLOAD[qlen:]


def test_splitter_metadata_has_correct_fields():
    split = split_payload(PAYLOAD, _fake_decision(0.5))
    meta  = make_metadata(split)
    assert meta["quantum_len"]  == split.quantum_len
    assert meta["classical_len"] == split.classical_len
    assert meta["total_len"]    == split.total_len
    assert meta["quantum_len"] + meta["classical_len"] == meta["total_len"]


def test_splitter_reassembly_length_validation():
    split = split_payload(PAYLOAD, _fake_decision(0.5))
    meta  = make_metadata(split)
    with pytest.raises(ReassemblyError):
        reassemble_from_segments(
            b"wrong",          # wrong quantum length
            split.classical_segment,
            meta,
        )


def test_splitter_empty_payload():
    split = split_payload(b"", _fake_decision(0.5))
    assert split.quantum_segment   == b""
    assert split.classical_segment == b""
    assert split.total_len == 0
    reconstructed = reassemble_from_segments(b"", b"", make_metadata(split))
    assert reconstructed == b""


# ---------------------------------------------------------------------------
# 2. Quantum transmit unit tests
# ---------------------------------------------------------------------------

def test_ebits_needed_formula():
    assert ebits_needed(0)  == 0
    assert ebits_needed(1)  == 4    # 1 byte × 4 ebits/byte
    assert ebits_needed(10) == 40
    assert ebits_needed(6)  == 24


def test_sdc_encode_decode_identity():
    """encode_bytes_sdc is identity in noiseless simulation."""
    data = b"\x00\xFF\xAB\x55"   # various bit patterns
    result = encode_bytes_sdc(data, available_ebits=100)
    print(f"\n[quantum_tx] input={data.hex()}  output={result.hex()}")
    assert result == data


def test_sdc_encode_decode_all_bytes():
    """Verify the bit packing round-trips all 256 byte values."""
    data = bytes(range(256))
    result = encode_bytes_sdc(data, available_ebits=256 * 4)
    assert result == data
    print(f"\n[quantum_tx] 256-byte round-trip ✓")


def test_sdc_encode_empty():
    result = encode_bytes_sdc(b"", available_ebits=100)
    assert result == b""


def test_ebit_capacity_error_raised():
    data = b"A" * 10   # needs 40 ebits
    with pytest.raises(EbitCapacityError) as exc_info:
        encode_bytes_sdc(data, available_ebits=5)
    print(f"\n[ebit_cap] raised: {exc_info.value}")
    assert "40" in str(exc_info.value)   # mentions required count
    assert "5"  in str(exc_info.value)   # mentions available count


def test_ebit_capacity_exact_boundary():
    """Exactly enough ebits → should NOT raise."""
    data = b"B" * 5    # needs exactly 20 ebits
    result = encode_bytes_sdc(data, available_ebits=20)
    assert result == data


def test_ebit_capacity_one_short_raises():
    data = b"C" * 5    # needs 20 ebits
    with pytest.raises(EbitCapacityError):
        encode_bytes_sdc(data, available_ebits=19)


# ---------------------------------------------------------------------------
# 3. Classical transmit unit test (uses socketpair for no-port-allocation)
# ---------------------------------------------------------------------------

def test_classical_transmit_round_trip():
    """send_classical_segment / recv_classical_segment round-trip over socketpair."""
    aes_key   = _qkd_key()
    plaintext = b"classical channel test payload"

    server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

    class _SimpleDC:
        def __init__(self, sock):
            self._conn = sock
        def send(self, data):
            from classical.transport import send_frame
            send_frame(self._conn, data)
        def recv(self):
            from classical.transport import recv_frame
            return recv_frame(self._conn)

    dc_a = _SimpleDC(server_sock)
    dc_b = _SimpleDC(client_sock)

    send_classical_segment(plaintext, aes_key, dc_a)
    received = recv_classical_segment(dc_b, aes_key)

    server_sock.close()
    client_sock.close()

    assert received == plaintext
    print(f"\n[classical_tx] round-trip: {plaintext!r} ✓")


# ---------------------------------------------------------------------------
# Integration + concurrency helpers
# ---------------------------------------------------------------------------

def _run_full_transmission(config: dict, payload: bytes) -> tuple[bytes | None, dict]:
    """
    Full pipeline: QKD → split (Phase 4) → dual-channel transmit → reassemble.
    Returns (reassembled_payload, errors_dict).
    """
    server = EbitServer(config)
    server.start()
    server.ready.wait(timeout=5)

    node_a = Node("A", config)
    node_b = Node("B", config)
    errors: dict[str, Exception] = {}

    b_registered    = threading.Event()
    b_channels_ready = threading.Event()  # both DCs bound and listening

    def run_b() -> None:
        try:
            node_b.connect()
            node_b.register()
            b_registered.set()
            node_b.wait_for_connection()
            node_b.run_qkd()
            aes_key = derive_key(node_b.session.key)

            # Open both listeners
            c_dc = DataChannel(config["host"], config["data_port"])
            q_dc = DataChannel(config["host"], config["quantum_port"])
            c_dc.listen()
            q_dc.listen()
            b_channels_ready.set()

            # Accept both connections (order doesn't matter — both will connect)
            c_dc.accept()
            q_dc.accept()

            node_b.receive_payload(c_dc, q_dc, aes_key)
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
                security_level  = config["security_level"],
                payload_size_bytes = len(payload),
                available_ebits = config["available_ebits"],
                qber            = node_a.session.qber,
            )
            split = split_payload(payload, decision)
            print(f"\n[integration] decision={decision.reason}  "
                  f"q_len={split.quantum_len}  c_len={split.classical_len}  "
                  f"ebits_needed={ebits_needed(split.quantum_len)}")

            node_a.transmit_payload(payload, decision, c_dc, q_dc, aes_key,
                                     config["available_ebits"])
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

    return node_b.last_received_payload, errors


# ---------------------------------------------------------------------------
# 4. Integration test
# ---------------------------------------------------------------------------

INTEGRATION_PAYLOAD = b"hbd-test-p5!"   # 12 bytes; medium→6 q bytes, 24 ebits ≤ 50


def test_integration_payload_matches():
    config = _make_config(_free_port(), _free_port(), _free_port())
    received, errors = _run_full_transmission(config, INTEGRATION_PAYLOAD)

    print(f"\n[integration] errors={errors}")
    print(f"  sent    : {INTEGRATION_PAYLOAD!r}")
    print(f"  received: {received!r}")

    assert not errors, f"Errors: {errors}"
    assert received == INTEGRATION_PAYLOAD, (
        f"Payload mismatch!\n  sent    : {INTEGRATION_PAYLOAD}\n  received: {received}"
    )


def test_integration_byte_for_byte():
    """Each byte of the reassembled payload must match the original exactly."""
    config = _make_config(_free_port(), _free_port(), _free_port())
    received, errors = _run_full_transmission(config, INTEGRATION_PAYLOAD)

    assert not errors
    for i, (orig, recv) in enumerate(zip(INTEGRATION_PAYLOAD, received)):
        assert orig == recv, f"Byte {i}: expected {orig:#04x}, got {recv:#04x}"


def test_integration_all_classical_payload():
    """Edge case: quantum_fraction=0.0 — everything goes classical."""
    payload = b"all-classical-path-test"
    config  = _make_config(_free_port(), _free_port(), _free_port())
    config["security_level"] = "low"

    # Override split decision via very low available_ebits (forces 0% quantum)
    config["available_ebits"] = 0
    received, errors = _run_full_transmission(config, payload)

    assert not errors
    assert received == payload
    print(f"\n[integration all-classical] {payload!r} ✓")


# ---------------------------------------------------------------------------
# 5. Concurrency check
# ---------------------------------------------------------------------------

def test_concurrent_transmission_overlaps():
    """
    Classical transmission (AES + socket, nearly instant) must start
    BEFORE quantum transmission (SDC circuits, slower) has finished.

    Assertion: timings["c_start"] < timings["q_end"]

    In sequential execution (quantum first, then classical):
      q_start → q_end → c_start → c_end
      → c_start > q_end  → assertion FAILS (correctly detecting non-concurrency)

    In concurrent execution:
      q_start ≈ c_start (both threads launched together); q_end >> c_end
      → c_start < q_end  → assertion PASSES
    """
    config = _make_config(_free_port(), _free_port(), _free_port())
    received, errors = _run_full_transmission(config, INTEGRATION_PAYLOAD)

    assert not errors
    timings = None
    # node_a is not accessible here; rerun with direct access
    # Use a separate small test that captures node_a directly:
    # (the integration helper doesn't expose node_a timings; use the
    #  lower-level approach below)
    print("\n[concurrency] Checked via dedicated sub-test below")


def test_concurrent_tx_timings_overlap():
    """
    Lower-level concurrency test: directly call transmit_payload and inspect
    the timing dict stored on the node.
    """
    import socket as _socket

    aes_key = _qkd_key()
    payload = b"concurrency-check-payload"

    # Build a minimal split decision: 50% quantum, 50% classical
    decision = _fake_decision(0.5)
    from transmission.payload_splitter import split_payload as _sp
    split = _sp(payload, decision)

    q_len = split.quantum_len   # 12 bytes → 48 ebits needed
    # Use enough available_ebits for the quantum segment
    available = max(ebits_needed(q_len) + 10, 50)

    print(f"\n[concurrency] quantum_len={q_len}B, ebits_needed={ebits_needed(q_len)}, "
          f"available={available}")

    # Set up loopback socket pairs for both channels
    c_a_sock, c_b_sock = _socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    q_a_sock, q_b_sock = _socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

    class _InlineDC:
        def __init__(self, sock):
            self._conn = sock
        def send(self, data):
            from classical.transport import send_frame
            send_frame(self._conn, data)
        def recv(self):
            from classical.transport import recv_frame
            return recv_frame(self._conn)
        def close(self):
            self._conn.close()

    c_dc_a = _InlineDC(c_a_sock)
    q_dc_a = _InlineDC(q_a_sock)
    c_dc_b = _InlineDC(c_b_sock)
    q_dc_b = _InlineDC(q_b_sock)

    node_a = Node("A_timing", {
        "node_id": "A_timing",
        "host": "127.0.0.1",
        "server_port": 0,
        "injected_qber": 0.0,
        "protocol": "BB84",
    })

    # Bob side: drain both channels concurrently so node_a doesn't block
    received_q = [None]
    received_c = [None]

    def drain_b():
        meta_raw = c_dc_b.recv()
        meta = json.loads(meta_raw.decode())
        tq = threading.Thread(target=lambda: received_q.__setitem__(0, q_dc_b.recv()))
        tc = threading.Thread(target=lambda: received_c.__setitem__(0, recv_classical_segment(c_dc_b, aes_key)))
        tq.start(); tc.start()
        tq.join(); tc.join()

    drain_thread = threading.Thread(target=drain_b, daemon=True)
    drain_thread.start()

    node_a.transmit_payload(payload, decision, c_dc_a, q_dc_a, aes_key, available)
    drain_thread.join(timeout=60)

    timings = node_a._last_tx_timings
    print(f"  q_start={timings['q_start']:.6f}  q_end={timings['q_end']:.6f}  "
          f"duration={timings['q_end']-timings['q_start']:.4f}s")
    print(f"  c_start={timings['c_start']:.6f}  c_end={timings['c_end']:.6f}  "
          f"duration={timings['c_end']-timings['c_start']:.4f}s")

    for dc in [c_dc_a, q_dc_a, c_dc_b, q_dc_b]:
        dc.close()

    assert "q_start" in timings and "q_end" in timings, "Quantum timing not captured"
    assert "c_start" in timings and "c_end" in timings, "Classical timing not captured"

    overlap = timings["c_start"] < timings["q_end"]
    print(f"  overlap (c_start < q_end): {overlap}")
    assert overlap, (
        f"Channels were NOT concurrent: classical started after quantum ended.\n"
        f"  c_start={timings['c_start']:.6f}  q_end={timings['q_end']:.6f}"
    )


# ---------------------------------------------------------------------------
# 6. Failure test — EbitCapacityError propagates from transmit_payload
# ---------------------------------------------------------------------------

def test_transmit_raises_on_ebit_overflow():
    """
    If the quantum segment needs more ebits than available, transmit_payload
    must raise EbitCapacityError rather than silently sending corrupted data.

    This tests that Phase 4's sizing guarantee is enforced at Phase 5's boundary.
    """
    import socket as _socket

    aes_key = _qkd_key()
    payload = b"A" * 20   # 20 bytes; 0.5 quantum → 10-byte q segment → 40 ebits

    decision      = _fake_decision(0.5)
    too_few_ebits = 5   # 5 << 40 needed

    # Create loopback DCs
    c_a, c_b = _socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    q_a, q_b = _socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

    class _DC:
        def __init__(self, s): self._conn = s
        def send(self, d):
            from classical.transport import send_frame; send_frame(self._conn, d)
        def recv(self):
            from classical.transport import recv_frame; return recv_frame(self._conn)
        def close(self): self._conn.close()

    c_dc_a = _DC(c_a); q_dc_a = _DC(q_a)

    node_a = Node("A_fail", {"host": "127.0.0.1", "server_port": 0, "injected_qber": 0.0, "protocol": "BB84"})

    with pytest.raises(EbitCapacityError) as exc_info:
        node_a.transmit_payload(payload, decision, c_dc_a, q_dc_a, aes_key, too_few_ebits)

    print(f"\n[ebit_overflow] raised: {exc_info.value}")

    for s in [c_a, c_b, q_a, q_b]:
        s.close()

    # Verify it mentions the mismatch
    msg = str(exc_info.value)
    assert "40" in msg, f"Error should mention required ebits (40), got: {msg}"
    assert "5"  in msg, f"Error should mention available ebits (5), got: {msg}"
