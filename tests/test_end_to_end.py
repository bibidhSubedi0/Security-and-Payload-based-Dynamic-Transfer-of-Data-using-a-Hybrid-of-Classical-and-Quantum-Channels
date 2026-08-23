"""
Phase 8 Part A — Full end-to-end integration tests.

Purpose
-------
Exercise the ENTIRE pipeline with no phase mocked:
  server startup → node discovery → connection handshake → ebit distribution →
  real BB84 / E91 QKD → split controller decision → concurrent dual-channel
  transmission → echo validation → metrics record written to transfers.jsonl.

Two tests:
  1. BB84 protocol end-to-end
  2. E91 protocol end-to-end

Both assert:
  - Node B's final payload == Node A's original payload (byte-for-byte)
  - A well-formed metrics JSON record exists with all required fields
  - SKR > 0, throughput > 0, latency > 0

Integration gaps found during wiring
--------------------------------------
No data-shape or field-name mismatch was found between phases.  Specifics:

- alice_key / bob_key symmetry: the EbitServer derives alice_key from
  bb84.alice_bits and bob_key from bb84.bob_results at matching-basis positions.
  In noiseless simulation, bob_results[i] == alice_bits[i] at all matching
  positions, so both nodes derive IDENTICAL AES keys → AES-GCM succeeds.

- MetricsCollector.record_transfer() reads echo_result.echo_diagnosis directly;
  EchoResult.diagnosis was renamed to echo_diagnosis at the source
  (transmission/echo_validation.py) and the old hasattr shim removed.

- E91 key generation falls through to BB84 after CHSH check — same key-derivation
  path, same noiseless guarantee → keys match.  Verified here.

- Session timing: QKD elapsed is measured at the call site (time.monotonic()
  before/after node.run_qkd()), not inside node.py, consistent with Phase 7.
"""

import json
import pathlib
import socket
import tempfile

import pytest

import sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from quantum_demo.pipeline import run_session


# ---------------------------------------------------------------------------
# Required fields (matches MetricsCollector schema v1.0)
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = {
    "schema_version", "session_id", "transfer_id", "timestamp_utc",
    "protocol", "qber", "chsh", "qkd_key_bits", "qkd_elapsed_s",
    "skr_bits_per_second",
    "split_reason", "quantum_fraction", "classical_fraction",
    "payload_bytes", "quantum_bytes", "classical_bytes",
    "transfer_elapsed_s", "throughput_bytes_per_s", "latency_s",
    "echo_outcome", "echo_recovered", "mismatch_source", "echo_diagnosis",
    "retransmit_bytes",
    "bits_corrected", "bits_sacrificed",
    "fault_injection_enabled", "ber_simulated", "snr_db_simulated",
    "plr_simulated", "bits_injected_errors", "packets_simulated_dropped",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_config(
    server_port: int,
    data_port: int,
    quantum_port: int,
    protocol: str = "BB84",
    security_level: str = "medium",
    payload_size_bytes: int = 16,
    available_ebits: int = 100,
) -> dict:
    return {
        "host": "127.0.0.1",
        "server_port": server_port,
        "data_port": data_port,
        "quantum_port": quantum_port,
        "protocol": protocol,
        "injected_qber": 0.0,
        "n_qubits": 200,
        "n_pairs_per_setting": 100,   # fast E91
        "security_level": security_level,
        "payload_size_bytes": payload_size_bytes,
        "available_ebits": available_ebits,
    }


def _run_full_pipeline(
    config: dict,
    payload: bytes,
    log_path: pathlib.Path,
) -> tuple[bytes, dict]:
    """
    Run the complete pipeline from server startup to metrics record via the
    extracted quantum_demo.pipeline.run_session() module.

    Returns (bob_final_payload, json_record_dict).
    """
    result = run_session(config, payload=payload, metrics_log_path=log_path)

    if result.outcome != "CLEAN_PASS":
        raise RuntimeError(
            f"Pipeline outcome {result.outcome}: {result.abort_reason}"
        )

    # Read back the metrics record that run_session persisted
    obj = json.loads(pathlib.Path(log_path).read_text().strip().splitlines()[-1])
    return result.bob_payload, obj


# ---------------------------------------------------------------------------
# Test 1 — BB84 full pipeline
# ---------------------------------------------------------------------------

def test_e2e_bb84_full_pipeline():
    """
    BB84 end-to-end: server → QKD → split → dual-channel transmit → echo → metrics.
    Payload Node B receives must match exactly what Node A sent.
    """
    payload = b"hbd-e2e-bb84-test-payload!"   # 26 bytes
    config  = _make_config(
        _free_port(), _free_port(), _free_port(),
        protocol="BB84",
        security_level="medium",
        available_ebits=200,
    )

    with tempfile.TemporaryDirectory() as tmp:
        log_path = pathlib.Path(tmp) / "e2e_bb84.jsonl"
        bob_payload, rec = _run_full_pipeline(config, payload, log_path)

    # --- Payload integrity ---
    assert bob_payload == payload, (
        f"Payload mismatch!\n  sent    : {payload!r}\n  received: {bob_payload!r}"
    )

    # --- Metrics record shape ---
    missing = REQUIRED_FIELDS - set(rec.keys())
    assert not missing, f"Metrics record missing fields: {missing}"

    # --- Sanity values ---
    assert rec["protocol"] == "BB84"
    assert rec["qber"] == 0.0
    assert rec["chsh"] is None
    assert rec["qkd_key_bits"] > 0
    assert rec["skr_bits_per_second"] > 0,  "SKR must be positive"
    assert rec["throughput_bytes_per_s"] > 0, "Throughput must be positive"
    assert rec["latency_s"] > 0,             "Latency must be positive"
    assert rec["echo_outcome"] == "CLEAN_PASS"
    assert rec["echo_recovered"] is False
    assert rec["payload_bytes"] == len(payload)
    assert rec["quantum_bytes"] + rec["classical_bytes"] == rec["payload_bytes"]

    print(f"\n[e2e_bb84] payload={payload!r}")
    print(f"  QBER={rec['qber']}  SKR={rec['skr_bits_per_second']:.1f} bps")
    print(f"  split={rec['split_reason']}  q={rec['quantum_bytes']}B  c={rec['classical_bytes']}B")
    print(f"  throughput={rec['throughput_bytes_per_s']:.1f} B/s  latency={rec['latency_s']*1000:.1f} ms")
    print(f"  echo_outcome={rec['echo_outcome']} ✓")


# ---------------------------------------------------------------------------
# Test 2 — E91 full pipeline
# ---------------------------------------------------------------------------

def test_e2e_e91_full_pipeline():
    """
    E91 end-to-end: CHSH verification → BB84 key generation sub-step →
    split → dual-channel transmit → echo → metrics.
    The E91-specific fields (chsh) must be present and valid in the record.
    """
    payload = b"hbd-e2e-e91!"   # 12 bytes
    config  = _make_config(
        _free_port(), _free_port(), _free_port(),
        protocol="E91",
        security_level="low",
        available_ebits=200,
        payload_size_bytes=12,
    )

    with tempfile.TemporaryDirectory() as tmp:
        log_path = pathlib.Path(tmp) / "e2e_e91.jsonl"
        bob_payload, rec = _run_full_pipeline(config, payload, log_path)

    # --- Payload integrity ---
    assert bob_payload == payload, (
        f"E91 payload mismatch!\n  sent    : {payload!r}\n  received: {bob_payload!r}"
    )

    # --- Metrics record shape ---
    missing = REQUIRED_FIELDS - set(rec.keys())
    assert not missing, f"Metrics record missing fields: {missing}"

    # --- E91-specific checks ---
    assert rec["protocol"] == "E91"
    assert rec["chsh"] is not None,        "E91 must record the CHSH S parameter"
    assert abs(rec["chsh"]) > 2.0,         "CHSH S must exceed classical bound (|S|>2)"
    # Tsirelson bound = 2√2 ≈ 2.828. With n_pairs_per_setting=100, σ(S) ≈ √(2/100)
    # ≈ 0.14, so 3.5 is ~4.8σ above the theoretical mean — admits all realistic
    # finite-sample outcomes while rejecting a broken implementation.
    assert abs(rec["chsh"]) <= 3.5,        "CHSH S must not exceed quantum bound (~2√2 + finite-sample margin)"
    assert rec["qkd_key_bits"] > 0
    assert rec["skr_bits_per_second"] > 0
    assert rec["echo_outcome"] == "CLEAN_PASS"
    assert bob_payload == payload

    print(f"\n[e2e_e91] payload={payload!r}")
    print(f"  CHSH S={rec['chsh']:.4f}  (quantum bound ≈ 2.828)")
    print(f"  SKR={rec['skr_bits_per_second']:.1f} bps")
    print(f"  echo_outcome={rec['echo_outcome']} ✓")
