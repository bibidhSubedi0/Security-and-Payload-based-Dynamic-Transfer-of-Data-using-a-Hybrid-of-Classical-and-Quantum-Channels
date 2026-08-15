"""
Phase 3 classical channel tests.

Three categories:

1. Unit — encrypt/decrypt round-trip with a QKD-derived key (no network).
2. Integration — full Phase 2 QKD session → derive key → direct A↔B socket →
   Alice encrypts + sends, Bob receives + decrypts. No server involvement in
   the data transfer.
3. Tamper — corrupt a ciphertext byte, confirm AES-GCM InvalidTag is raised
   (proves the auth tag is actually being enforced, not silently ignored).

Data channel direction: Bob (receiver) listens; Alice (sender) connects.
Both know the port from config; no extra coordination messages needed.
"""

import socket
import threading

import pytest
from cryptography.exceptions import InvalidTag

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from classical.aes_channel import derive_key, encrypt, decrypt
from classical.transport import DataChannel
from quantum.bb84 import run_bb84
from server.ebit_server import EbitServer
from node.node import Node


# ---------------------------------------------------------------------------
# Helpers (shared with test_network.py pattern)
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_config(server_port: int, data_port: int, noise: float = 0.0) -> dict:
    return {
        "host": "127.0.0.1",
        "server_port": server_port,
        "data_port": data_port,
        "protocol": "BB84",
        "injected_qber": noise,
        "n_qubits": 200,
        "n_pairs_per_setting": 300,
        "security_level": "medium",
        "payload_size_bytes": 1024,
        "available_ebits": 50,
    }


def _qkd_key(seed: int = 42) -> bytes:
    """Convenience: run BB84 locally, derive AES key. Used by unit tests."""
    result = run_bb84(n_qubits=200, injected_noise=0.0, seed=seed)
    return derive_key(result.sifted_key)


# ---------------------------------------------------------------------------
# 1. Unit tests — encrypt / decrypt (no network)
# ---------------------------------------------------------------------------

def test_derive_key_length():
    key = _qkd_key()
    print(f"\n[unit] derived key length={len(key)} bytes ({len(key)*8} bits)")
    assert len(key) == 32, f"Expected 32-byte key, got {len(key)}"


def test_derive_key_deterministic():
    """Same QKD bits → same AES key."""
    result = run_bb84(n_qubits=200, injected_noise=0.0, seed=7)
    k1 = derive_key(result.sifted_key)
    k2 = derive_key(result.sifted_key)
    assert k1 == k2


def test_roundtrip_short_payload():
    key = _qkd_key(seed=1)
    plaintext = b"hello quantum world"
    ciphertext = encrypt(plaintext, key)
    recovered = decrypt(ciphertext, key)
    print(f"\n[unit] plaintext={plaintext!r}  ciphertext_len={len(ciphertext)}")
    assert recovered == plaintext


def test_roundtrip_empty_payload():
    key = _qkd_key(seed=2)
    plaintext = b""
    ciphertext = encrypt(plaintext, key)
    recovered = decrypt(ciphertext, key)
    assert recovered == plaintext


def test_roundtrip_large_payload():
    key = _qkd_key(seed=3)
    plaintext = bytes(range(256)) * 64   # 16 KiB
    ciphertext = encrypt(plaintext, key)
    recovered = decrypt(ciphertext, key)
    assert recovered == plaintext
    print(f"\n[unit] large payload: {len(plaintext)} bytes plain → "
          f"{len(ciphertext)} bytes ciphertext")


def test_ciphertext_is_not_plaintext():
    key = _qkd_key(seed=4)
    plaintext = b"secret"
    ciphertext = encrypt(plaintext, key)
    assert plaintext not in ciphertext


def test_different_calls_different_nonces():
    """encrypt() must use a fresh nonce each call; identical plaintexts → different ciphertexts."""
    key = _qkd_key(seed=5)
    plaintext = b"same message"
    c1 = encrypt(plaintext, key)
    c2 = encrypt(plaintext, key)
    assert c1 != c2, "Two encrypt() calls produced identical output — nonce not random"


# ---------------------------------------------------------------------------
# 2. Tamper tests — GCM auth tag enforcement
# ---------------------------------------------------------------------------

def test_tamper_ciphertext_body_raises():
    """Flip a bit in the ciphertext body (past the 12-byte nonce); decrypt must raise."""
    key = _qkd_key(seed=10)
    plaintext = b"tamper me"
    ciphertext = encrypt(plaintext, key)

    corrupted = bytearray(ciphertext)
    corrupted[12] ^= 0xFF   # first byte of ciphertext body (index 12 = after 12-byte nonce)
    print(f"\n[tamper] original byte at [12]: {ciphertext[12]:#04x}  "
          f"corrupted: {corrupted[12]:#04x}")

    with pytest.raises(InvalidTag):
        decrypt(bytes(corrupted), key)


def test_tamper_auth_tag_raises():
    """Corrupt the last byte (end of the 16-byte GCM tag); decrypt must raise."""
    key = _qkd_key(seed=11)
    plaintext = b"tamper the tag"
    ciphertext = encrypt(plaintext, key)

    corrupted = bytearray(ciphertext)
    corrupted[-1] ^= 0x01
    print(f"\n[tamper] tag last byte original={ciphertext[-1]:#04x}  "
          f"corrupted={corrupted[-1]:#04x}")

    with pytest.raises(InvalidTag):
        decrypt(bytes(corrupted), key)


def test_tamper_nonce_raises():
    """Corrupt a byte of the 12-byte nonce; decrypt will attempt wrong nonce → bad tag."""
    key = _qkd_key(seed=12)
    plaintext = b"tamper nonce"
    ciphertext = encrypt(plaintext, key)

    corrupted = bytearray(ciphertext)
    corrupted[0] ^= 0xFF   # first byte of nonce
    with pytest.raises(InvalidTag):
        decrypt(bytes(corrupted), key)


def test_wrong_key_raises():
    """Decrypting with a different key must raise InvalidTag."""
    key_a = _qkd_key(seed=13)
    key_b = _qkd_key(seed=14)  # different seed → different QKD bits → different key
    assert key_a != key_b
    plaintext = b"wrong key test"
    ciphertext = encrypt(plaintext, key_a)
    with pytest.raises(InvalidTag):
        decrypt(ciphertext, key_b)


# ---------------------------------------------------------------------------
# 3. Integration test — full QKD → derive key → direct socket → encrypt/send/recv/decrypt
# ---------------------------------------------------------------------------

def _run_full_session(
    config: dict,
    plaintext: bytes,
) -> tuple[bytes, dict[str, Exception]]:
    """
    Spin up server + Node A + Node B, run QKD, then transfer an encrypted payload
    over the direct A↔B data channel (no server involvement in data transfer).

    Returns (decrypted_at_bob, errors_dict).
    """
    server = EbitServer(config)
    server.start()
    server.ready.wait(timeout=5)

    node_a = Node("A", config)
    node_b = Node("B", config)
    errors: dict[str, Exception] = {}
    results: dict[str, bytes] = {}

    b_registered  = threading.Event()
    b_data_ready  = threading.Event()   # set once Bob's listener is bound

    def run_b() -> None:
        try:
            node_b.connect()
            node_b.register()
            b_registered.set()
            node_b.wait_for_connection()
            node_b.run_qkd()

            aes_key = derive_key(node_b.session.key)

            # Bob opens data listener — Bob listens, Alice connects
            dc = DataChannel(config["host"], config["data_port"])
            dc.listen()
            b_data_ready.set()   # port is bound; Alice can now connect

            dc.accept()          # block until Alice connects
            raw = dc.recv()
            results["B"] = decrypt(raw, aes_key)
            dc.close()
        except Exception as exc:
            errors["B"] = exc
            b_registered.set()
            b_data_ready.set()
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

            # Wait for Bob's listener to be ready before connecting
            b_data_ready.wait(timeout=10)

            # Alice connects directly to Bob — no server relay
            dc = DataChannel(config["host"], config["data_port"])
            dc.connect()
            dc.send(encrypt(plaintext, aes_key))
            dc.close()
        except Exception as exc:
            errors["A"] = exc
        finally:
            node_a.close()

    tb = threading.Thread(target=run_b, daemon=True)
    ta = threading.Thread(target=run_a, daemon=True)
    tb.start()
    ta.start()
    tb.join(timeout=60)
    ta.join(timeout=60)
    server.stop()

    return results.get("B"), errors


def test_integration_plaintext_matches():
    """End-to-end: QKD key → encrypt on Alice → decrypt on Bob → same plaintext."""
    plaintext = b"hybrid quantum-classical channel test"
    config = _make_config(_free_port(), _free_port())

    received, errors = _run_full_session(config, plaintext)

    print(f"\n[integration] errors={errors}")
    print(f"  sent    : {plaintext!r}")
    print(f"  received: {received!r}")
    assert not errors, f"Session errors: {errors}"
    assert received == plaintext


def test_integration_binary_payload():
    """Payload with arbitrary binary bytes (not valid UTF-8)."""
    plaintext = bytes(range(256))
    config = _make_config(_free_port(), _free_port())
    received, errors = _run_full_session(config, plaintext)
    assert not errors
    assert received == plaintext
    print(f"\n[integration binary] {len(plaintext)}-byte payload round-tripped correctly")


def test_integration_large_payload():
    """Payload larger than a single TCP segment — exercises the framing logic."""
    plaintext = b"Q" * 65536   # 64 KiB
    config = _make_config(_free_port(), _free_port())
    received, errors = _run_full_session(config, plaintext)
    assert not errors
    assert received == plaintext
    print(f"\n[integration large] {len(plaintext)}-byte payload received correctly")


def test_integration_keys_match_between_nodes():
    """Verify both nodes derived the same AES key from their QKD session keys."""
    config = _make_config(_free_port(), _free_port())
    server = EbitServer(config)
    server.start()
    server.ready.wait(timeout=5)

    node_a = Node("A", config)
    node_b = Node("B", config)
    errors: dict[str, Exception] = {}
    keys: dict[str, bytes] = {}
    b_registered = threading.Event()

    def run_b() -> None:
        try:
            node_b.connect()
            node_b.register()
            b_registered.set()
            node_b.wait_for_connection()
            node_b.run_qkd()
            keys["B"] = derive_key(node_b.session.key)
        except Exception as exc:
            errors["B"] = exc
            b_registered.set()
        finally:
            node_b.close()

    def run_a() -> None:
        try:
            b_registered.wait(timeout=10)
            node_a.connect()
            node_a.register()
            node_a.wait_for_peer("B", timeout=10)
            node_a.request_connection("B")
            node_a.run_qkd()
            keys["A"] = derive_key(node_a.session.key)
        except Exception as exc:
            errors["A"] = exc
        finally:
            node_a.close()

    tb = threading.Thread(target=run_b, daemon=True)
    ta = threading.Thread(target=run_a, daemon=True)
    tb.start()
    ta.start()
    tb.join(timeout=60)
    ta.join(timeout=60)
    server.stop()

    assert not errors, f"Errors: {errors}"
    print(f"\n[integration keys] A_key={keys['A'].hex()[:16]}...  "
          f"B_key={keys['B'].hex()[:16]}...  "
          f"match={keys['A'] == keys['B']}")
    assert keys["A"] == keys["B"], "Alice and Bob derived different AES keys"
