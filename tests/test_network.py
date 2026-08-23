"""
Phase 2 network integration tests.

Thread model
------------
Each test spins up three threads:
  - server_thread  : daemon, runs EbitServer.start() then blocks (accept loop is internal)
  - node_b_thread  : registers, waits for connection, runs QKD, stores result
  - node_a_thread  : waits until B visible, requests connection, runs QKD, stores result

All threads share result / error dicts inspected by the main (pytest) thread after join.
Ports are allocated dynamically per test to avoid conflicts between parallel runs.

Why threads not processes?
  AerSimulator runs fine in threads (independent circuit objects). Threads share
  address space so we can directly inspect Node.session without IPC. No pickling of
  Qiskit objects required. For Phase 3+ (AES/sockets), processes would add isolation
  but threads are sufficient here.
"""

import socket
import threading
import time

import pytest
import yaml

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from server.ebit_server import EbitServer
from node.node import Node, SessionAbortedError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Return an ephemeral port that is currently free."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_config(port: int, protocol: str = "BB84", noise: float = 0.0) -> dict:
    return {
        "host": "127.0.0.1",
        "server_port": port,
        "protocol": protocol,
        "injected_qber": noise,
        "n_qubits": 200,
        "n_pairs_per_setting": 300,
        "security_level": "medium",
        "payload_size_bytes": 1024,
        "available_ebits": 50,
    }


def _run_session(
    config: dict,
    noise_override: float | None = None,
) -> tuple[Node, Node, dict[str, Exception]]:
    """
    Start server + Node A + Node B, run full discovery→connection→QKD flow.

    Returns (node_a, node_b, errors).
    errors is empty on success; contains "A" or "B" key on failure.
    """
    server = EbitServer(config)
    server.start()
    server.ready.wait(timeout=5)

    node_a = Node("A", config)
    node_b = Node("B", config)
    errors: dict[str, Exception] = {}

    b_registered = threading.Event()

    def run_b() -> None:
        try:
            node_b.connect()
            node_b.register()
            b_registered.set()
            node_b.wait_for_connection()
            node_b.run_qkd(injected_noise=noise_override)
        except Exception as exc:
            errors["B"] = exc
            b_registered.set()   # unblock A even on B failure

    def run_a() -> None:
        try:
            b_registered.wait(timeout=10)
            if "B" in errors:
                return
            node_a.connect()
            node_a.register()
            found = node_a.wait_for_peer("B", timeout=10)
            assert found, "Node B never appeared in registry"
            node_a.request_connection("B")
            node_a.run_qkd(injected_noise=noise_override)
        except Exception as exc:
            errors["A"] = exc

    tb = threading.Thread(target=run_b, daemon=True)
    ta = threading.Thread(target=run_a, daemon=True)
    tb.start()
    ta.start()
    tb.join(timeout=60)
    ta.join(timeout=60)

    node_a.close()
    node_b.close()
    server.stop()

    return node_a, node_b, errors


# ---------------------------------------------------------------------------
# Tests — happy path (noiseless BB84)
# ---------------------------------------------------------------------------

def test_both_nodes_complete_qkd():
    port = _free_port()
    config = _make_config(port, protocol="BB84", noise=0.0)
    node_a, node_b, errors = _run_session(config)

    print(f"\n[network] errors={errors}")
    assert not errors, f"Session errors: {errors}"
    assert node_a.session is not None, "Node A has no QKD session"
    assert node_b.session is not None, "Node B has no QKD session"


def test_qber_within_threshold_noiseless():
    port = _free_port()
    config = _make_config(port, protocol="BB84", noise=0.0)
    node_a, node_b, errors = _run_session(config)

    assert not errors
    print(f"\n[network qber] A.qber={node_a.session.qber:.4f}  "
          f"B.qber={node_b.session.qber:.4f}")
    assert node_a.session.qber < 0.11, (
        f"Node A QBER={node_a.session.qber:.4f} exceeds threshold in noiseless run"
    )
    assert node_b.session.qber < 0.11


def test_keys_match_noiseless():
    """In noiseless BB84 the sifted keys at both ends must be identical."""
    port = _free_port()
    config = _make_config(port, protocol="BB84", noise=0.0)
    node_a, node_b, errors = _run_session(config)

    assert not errors
    key_a = node_a.session.key
    key_b = node_b.session.key
    print(f"\n[network keys] len_A={len(key_a)}  len_B={len(key_b)}  "
          f"match={key_a == key_b}")
    assert len(key_a) > 0, "Alice's key is empty"
    assert key_a == key_b, (
        f"Keys do not match!\n  A={key_a[:20]}...\n  B={key_b[:20]}..."
    )


def test_roles_assigned_correctly():
    port = _free_port()
    config = _make_config(port, protocol="BB84", noise=0.0)
    node_a, node_b, errors = _run_session(config)

    assert not errors
    print(f"\n[network roles] A.role={node_a.session.role}  B.role={node_b.session.role}")
    assert node_a.session.role == "alice"
    assert node_b.session.role == "bob"


def test_key_nonempty_and_binary():
    port = _free_port()
    config = _make_config(port, protocol="BB84", noise=0.0)
    node_a, node_b, errors = _run_session(config)

    assert not errors
    key = node_a.session.key
    assert len(key) > 0
    assert all(b in (0, 1) for b in key), "Key contains non-binary values"


# ---------------------------------------------------------------------------
# Tests — abort path (high noise)
# ---------------------------------------------------------------------------

def test_high_noise_aborts_session():
    """
    Injecting 50% noise pushes QBER well above the 11% threshold.
    After MAX_RETRIES the server sends SESSION_ABORTED; both nodes must
    raise SessionAbortedError rather than returning a bad key.
    """
    port = _free_port()
    config = _make_config(port, protocol="BB84", noise=0.5)
    node_a, node_b, errors = _run_session(config)

    print(f"\n[network abort] errors={errors}")
    assert "A" in errors or "B" in errors, (
        "Expected SessionAbortedError on at least one node with 50% noise"
    )
    for side, exc in errors.items():
        print(f"  [{side}] {type(exc).__name__}: {exc}")
        assert isinstance(exc, SessionAbortedError), (
            f"Node {side} raised {type(exc).__name__} instead of SessionAbortedError"
        )
        assert exc.qber > 0.11, (
            f"Aborted session QBER={exc.qber:.4f} is below threshold — shouldn't have aborted"
        )


def test_abort_reason_is_qber():
    port = _free_port()
    config = _make_config(port, protocol="BB84", noise=0.5)
    node_a, node_b, errors = _run_session(config)

    for exc in errors.values():
        assert isinstance(exc, SessionAbortedError)
        assert exc.reason == "QBER_EXCEEDED", (
            f"Expected reason QBER_EXCEEDED, got {exc.reason!r}"
        )


def test_no_key_stored_after_abort():
    """Nodes must not expose a key object when the session aborted."""
    port = _free_port()
    config = _make_config(port, protocol="BB84", noise=0.5)
    node_a, node_b, errors = _run_session(config)

    if "A" in errors:
        assert node_a.session is None, "Node A stored a key despite abort"
    if "B" in errors:
        assert node_b.session is None, "Node B stored a key despite abort"


# ---------------------------------------------------------------------------
# Test — low noise stays secure (borderline sanity check)
# ---------------------------------------------------------------------------

def test_low_noise_session_succeeds():
    """5% noise is below threshold; session should complete successfully."""
    port = _free_port()
    config = _make_config(port, protocol="BB84", noise=0.05)
    node_a, node_b, errors = _run_session(config)

    print(f"\n[network low-noise] errors={errors}")
    print(f"  A.qber={node_a.session.qber if node_a.session else 'N/A'}  "
          f"B.qber={node_b.session.qber if node_b.session else 'N/A'}")

    # 5% noise → expected QBER ~2-5% — well within 11% threshold
    # Occasionally the first attempt might hit 11% and retry succeeds on attempt 2.
    # We only assert the session succeeded (no errors), not a specific attempt count.
    assert not errors, f"Session failed at 5% noise: {errors}"
    assert node_a.session is not None
    assert node_a.session.qber < 0.11
