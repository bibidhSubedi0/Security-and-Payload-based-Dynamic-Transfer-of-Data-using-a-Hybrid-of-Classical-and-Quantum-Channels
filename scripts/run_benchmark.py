#!/usr/bin/env python3
"""
Phase 8 Part C — Benchmarking sweep script

Runs many transfers across a grid of configurations and uses Phase 7's
MetricsCollector to log each one to a timestamped .jsonl file.

Grid
----
  security_level  : ["low", "medium", "high"]          — 3 values
  payload_size_B  : [16, 128, 512]                      — 3 values (small/medium/large)
  injected_qber   : [0.0, 0.05, 0.12]                   — 3 values
                    0.0  → noiseless channel
                    0.05 → moderate degradation (QBER < 0.11 → transfers proceed)
                    0.12 → above abort threshold (QBER > 0.11 → SESSION_ABORTED)
  fault_injection : [off, BER=0.05]                     — 2 values

Total: 3 × 3 × 3 × 2 = 54 configurations.

Speed notes
-----------
n_qubits=200 (matches the system default used in Phase 1-7 tests).
n_pairs_per_setting=50 (E91 CHSH verification runs, not used here — BB84 only).
Sequential execution to keep memory stable.

Output
------
  metrics/logs/benchmark_<YYYYMMDD_HHMMSS>.jsonl   — one JSON line per config
  Console summary printed at the end.

Usage
-----
  conda run -n hcq_proj python scripts/run_benchmark.py
"""

from __future__ import annotations

import json
import pathlib
import socket
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime

# Ensure project root is on sys.path when run from any directory
_SCRIPT_DIR  = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from classical.aes_channel import derive_key
from classical.fault_injector import FaultInjector
from classical.transport import DataChannel
from metrics.collector import MetricsCollector
from node.node import Node, SessionAbortedError
from quantum.reconciliation import (
    reconcile_alice, reconcile_bob,
    verify_key_alice, verify_key_bob,
    ReconciliationIncompleteError,
)
from server.ebit_server import EbitServer
from split_controller.controller import compute_split
from transmission.echo_validation import alice_transfer, bob_transfer
from transmission.payload_splitter import split_payload


# ---------------------------------------------------------------------------
# Grid definition
# ---------------------------------------------------------------------------

N_QUBITS = 200          # matches system default (Phase 1-7 tests)

SECURITY_LEVELS = ["low", "medium", "high"]

PAYLOAD_SIZES = [
    (16,  "small"),   # 16 B  — typical short control message
    (128, "medium"),  # 128 B — small data packet
    (512, "large"),   # 512 B — moderate payload; max quantum = 384 B = 1536 ebits
]

NOISE_LEVELS = [
    (0.00, "noiseless"),
    (0.05, "moderate"),
    (0.12, "above_threshold"),   # > BB84_QBER_THRESHOLD (0.11) → abort
]

FAULT_CONFIGS = [
    ({"fault_injection": {"enabled": False}},                        "off"),
    ({"fault_injection": {"enabled": True, "bit_error_probability": 0.05,
                          "packet_loss_probability": 0.0}},           "BER5%"),
]

# -------
# Total configs = 3 × 3 × 3 × 2 = 54
# -------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_config(server_port: int, data_port: int, quantum_port: int,
                 security_level: str, available_ebits: int,
                 noise: float) -> dict:
    return {
        "host": "127.0.0.1",
        "server_port": server_port,
        "data_port": data_port,
        "quantum_port": quantum_port,
        "protocol": "BB84",
        "injected_qber": noise,
        "n_qubits": N_QUBITS,
        "n_pairs_per_setting": 50,
        "security_level": security_level,
        "available_ebits": available_ebits,
    }


def run_one_transfer(
    config: dict,
    payload: bytes,
    fault_cfg: dict,
    timeout_s: float = 60.0,
) -> dict:
    """
    Run one complete transfer pipeline.

    Returns a result dict with:
      outcome: "CLEAN_PASS" | "RECOVERED_VIA_REROUTE" | "CHANNEL_FAILURE" | "SESSION_ABORTED"
      qber   : measured QBER (or the injected_qber if aborted before measurement)
      qkd_elapsed_s, transfer_elapsed_s
      node_a_session: QKDSession | None
      decision, split, echo_result, injector
    """
    server = EbitServer(config)
    server.start()
    server.ready.wait(timeout=5)

    node_a = Node("A", config)
    node_b = Node("B", config)

    errors: dict = {}
    b_registered     = threading.Event()
    b_channels_ready = threading.Event()

    node_a_ref:          list = [None]
    echo_result_h:       list = [None]
    qkd_elapsed_h:       list = [0.0]
    xfer_elapsed_h:      list = [0.0]
    decision_h:          list = [None]
    split_h:             list = [None]
    session_aborted:     list = [False]
    recon_incomplete:    list = [False]

    injector = FaultInjector(fault_cfg)

    def run_b():
        c_dc = None
        q_dc = None
        try:
            node_b.connect(); node_b.register()
            b_registered.set()
            node_b.wait_for_connection()
            node_b.run_qkd()

            c_dc = DataChannel(config["host"], config["data_port"])
            q_dc = DataChannel(config["host"], config["quantum_port"])
            c_dc.listen(); q_dc.listen()
            b_channels_ready.set()
            c_dc.accept(); q_dc.accept()

            # Reconciliation: correct residual bit errors before HKDF
            qber = max(node_b.session.qber, 1e-4)
            recon = reconcile_bob(node_b.session.key, qber, c_dc)
            if not recon.reconciled_bits:
                raise ReconciliationIncompleteError(
                    "No bits remain after privacy amplification"
                )
            aes_key = derive_key(recon.reconciled_bits)

            # Key verification: confirm keys match before touching the payload
            verify_key_bob(c_dc, aes_key)

            bob_transfer(node_b, c_dc, q_dc, aes_key)
            c_dc.close(); q_dc.close()
        except SessionAbortedError:
            b_registered.set(); b_channels_ready.set()
        except ReconciliationIncompleteError as exc:
            errors["B"] = exc
            b_registered.set(); b_channels_ready.set()
            if c_dc is not None:
                try: c_dc.close()
                except Exception: pass
            if q_dc is not None:
                try: q_dc.close()
                except Exception: pass
        except Exception as exc:
            errors["B"] = exc
            b_registered.set(); b_channels_ready.set()
            # Explicitly close data channels so Alice's dc.recv() unblocks
            # immediately instead of hanging until the per-config timeout.
            # This is the critical fix for the key-mismatch (InvalidTag) hang.
            if c_dc is not None:
                try: c_dc.close()
                except Exception: pass
            if q_dc is not None:
                try: q_dc.close()
                except Exception: pass
        finally:
            node_b.close()

    def run_a():
        try:
            b_registered.wait(timeout=15)
            if "B" in errors:
                return

            node_a.connect(); node_a.register()
            node_a.wait_for_peer("B", timeout=10)
            node_a.request_connection("B")

            t0 = time.monotonic()
            node_a.run_qkd()
            qkd_elapsed_h[0] = time.monotonic() - t0

            b_channels_ready.wait(timeout=10)

            c_dc = DataChannel(config["host"], config["data_port"])
            q_dc = DataChannel(config["host"], config["quantum_port"])
            c_dc.connect(); q_dc.connect()

            # Reconciliation: correct residual bit errors before HKDF
            qber = max(node_a.session.qber, 1e-4)
            recon = reconcile_alice(node_a.session.key, qber, c_dc)
            if not recon.reconciled_bits:
                raise ReconciliationIncompleteError(
                    "No bits remain after privacy amplification"
                )
            aes_key = derive_key(recon.reconciled_bits)

            # Key verification: confirm keys match before touching the payload
            verify_key_alice(c_dc, aes_key)

            decision = compute_split(
                security_level=config["security_level"],
                payload_size_bytes=len(payload),
                available_ebits=config["available_ebits"],
                qber=node_a.session.qber,
            )
            decision_h[0] = decision
            split_h[0]    = split_payload(payload, decision)

            # Apply fault injection to the classical segment (before transmission)
            classical_seg = split_h[0].classical_segment
            if injector.enabled and len(classical_seg) > 0:
                injector.process_plaintext(classical_seg)

            t1 = time.monotonic()
            result = alice_transfer(
                node_a, payload, decision, c_dc, q_dc, aes_key,
                config["available_ebits"], node_a.session.qber,
            )
            xfer_elapsed_h[0] = time.monotonic() - t1
            echo_result_h[0]  = result
            node_a_ref[0]     = node_a

            c_dc.close(); q_dc.close()
        except SessionAbortedError:
            session_aborted[0] = True
        except ReconciliationIncompleteError:
            recon_incomplete[0] = True
        except Exception as exc:
            errors["A"] = exc
        finally:
            node_a.close()

    tb = threading.Thread(target=run_b, daemon=True)
    ta = threading.Thread(target=run_a, daemon=True)
    tb.start(); ta.start()
    tb.join(timeout=timeout_s); ta.join(timeout=timeout_s)
    server.stop()

    if tb.is_alive() or ta.is_alive():
        return {
            "timed_out": True,
            "tb_alive":  tb.is_alive(),
            "ta_alive":  ta.is_alive(),
            "errors":    errors,
            "node_a":    node_a_ref[0],
        }

    return {
        "ok":               not errors and not session_aborted[0] and not recon_incomplete[0],
        "aborted":          session_aborted[0],
        "recon_incomplete": recon_incomplete[0],
        "errors":           errors,
        "node_a":           node_a_ref[0],
        "echo_result":      echo_result_h[0],
        "qkd_elapsed_s":    qkd_elapsed_h[0],
        "xfer_elapsed_s":   xfer_elapsed_h[0],
        "decision":         decision_h[0],
        "split":            split_h[0],
        "injector":         injector,
    }


def main() -> None:
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir   = _PROJECT_ROOT / "metrics" / "logs"
    log_path  = log_dir / f"benchmark_{ts}.jsonl"
    collector = MetricsCollector(log_path)
    session_id = str(uuid.uuid4())

    # Pre-compute total grid size
    configs = [
        (sec, (psz, plabel), (noise, nlabel), (fcfg, flabel))
        for sec   in SECURITY_LEVELS
        for (psz, plabel) in PAYLOAD_SIZES
        for (noise, nlabel) in NOISE_LEVELS
        for (fcfg, flabel) in FAULT_CONFIGS
    ]
    total = len(configs)

    print(f"\n{'='*62}", flush=True)
    print(f"  Phase 8 — Benchmark sweep  ({total} configurations)", flush=True)
    print(f"  Log: {log_path.relative_to(_PROJECT_ROOT)}", flush=True)
    print(f"{'='*62}\n", flush=True)

    counts = {"CLEAN_PASS": 0, "RECOVERED_VIA_REROUTE": 0,
              "CHANNEL_FAILURE": 0, "SESSION_ABORTED": 0,
              "RECONCILIATION_INCOMPLETE": 0,
              "KEY_MISMATCH_ERROR": 0, "ERROR": 0, "TIMEOUT": 0}
    sweep_start = time.monotonic()

    for idx, (sec, (psz, plabel), (noise, nlabel), (fcfg, flabel)) in enumerate(configs, 1):
        payload         = bytes(i % 256 for i in range(psz))
        assert len(payload) == psz, f"payload length bug: got {len(payload)}, want {psz}"
        available_ebits = psz * 4 * 2      # 2× the max possible quantum demand

        config = _make_config(
            _free_port(), _free_port(), _free_port(),
            security_level=sec,
            available_ebits=available_ebits,
            noise=noise,
        )

        tag = (f"[{idx:3d}/{total}] sec={sec:6s}  "
               f"payload={psz:4d}B({plabel:6s})  "
               f"noise={noise:.2f}({nlabel:16s})  "
               f"fault={flabel:5s}")

        # --- Step 1 diagnostic: pre-run progress print ---
        print(f"\n{tag}", flush=True)
        print(f"  → starting (timeout=60s)...", flush=True)

        t0 = time.monotonic()
        try:
            r = run_one_transfer(config, payload, fcfg, timeout_s=60.0)
        except Exception:
            elapsed = time.monotonic() - t0
            print(f"  → ERROR after {elapsed:.2f}s", flush=True)
            traceback.print_exc()
            counts["ERROR"] += 1
            continue

        elapsed = time.monotonic() - t0

        # --- Step 1 diagnostic: timeout path ---
        if r.get("timed_out"):
            counts["TIMEOUT"] = counts.get("TIMEOUT", 0) + 1
            na = r.get("node_a")
            tx = na._last_tx_timings if na else {}
            q_dur = tx.get("q_end", 0) - tx.get("q_start", 0)
            c_dur = tx.get("c_end", 0) - tx.get("c_start", 0)
            print(f"  → TIMEOUT after {elapsed:.2f}s  "
                  f"(tb_alive={r['tb_alive']} ta_alive={r['ta_alive']}  "
                  f"errors={r['errors']}  "
                  f"q_xfer={q_dur:.3f}s  c_xfer={c_dur:.4f}s)", flush=True)
            continue

        if r["aborted"]:
            outcome = "SESSION_ABORTED"
            counts[outcome] += 1
            print(f"  → {outcome:<22s} ({elapsed:.2f}s)", flush=True)
            continue

        if r["recon_incomplete"]:
            outcome = "RECONCILIATION_INCOMPLETE"
            counts[outcome] += 1
            print(f"  → {outcome:<22s} ({elapsed:.2f}s)", flush=True)
            continue

        if r["errors"]:
            # Key-mismatch: Bob's AES-GCM decrypt raises InvalidTag when
            # alice_key != bob_key (happens with small QBER sample + noise).
            b_exc = r["errors"].get("B")
            if b_exc is not None and type(b_exc).__name__ == "InvalidTag":
                outcome = "KEY_MISMATCH_ERROR"
            else:
                outcome = "ERROR"
            counts[outcome] += 1
            print(f"  → {outcome}: {r['errors']} ({elapsed:.2f}s)", flush=True)
            continue

        er   = r["echo_result"]
        if er.success and not er.recovered:
            outcome = "CLEAN_PASS"
        elif er.success and er.recovered:
            outcome = "RECOVERED_VIA_REROUTE"
        else:
            outcome = "CHANNEL_FAILURE"
        counts[outcome] += 1

        # Sub-timing breakdown (diagnostic)
        na = r["node_a"]
        tx = na._last_tx_timings if na else {}
        q_dur = tx.get("q_end", 0) - tx.get("q_start", 0)
        c_dur = tx.get("c_end", 0) - tx.get("c_start", 0)

        # Write metrics record
        try:
            collector.record_transfer(
                session_id        = session_id,
                protocol          = "BB84",
                qber              = na.session.qber,
                chsh              = na.session.chsh,
                qkd_key_bits      = len(na.session.key),
                qkd_elapsed_s     = r["qkd_elapsed_s"],
                decision          = r["decision"],
                payload_bytes     = psz,
                quantum_bytes     = r["split"].quantum_len,
                classical_bytes   = r["split"].classical_len,
                transfer_elapsed_s = r["xfer_elapsed_s"],
                echo_result       = er,
                fault_injector    = r["injector"],
            )
        except Exception as exc:
            print(f"  [WARNING] MetricsCollector write failed: {exc}", flush=True)

        print(f"  → {outcome:<22s} ({elapsed:.2f}s)  "
              f"qkd={r['qkd_elapsed_s']:.3f}s  "
              f"q_xfer={q_dur:.3f}s  c_xfer={c_dur:.4f}s  "
              f"qber={na.session.qber:.3f}", flush=True)

    total_elapsed = time.monotonic() - sweep_start

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    successful = sum(
        counts[k] for k in ("CLEAN_PASS", "RECOVERED_VIA_REROUTE", "CHANNEL_FAILURE")
    )
    print(f"\n{'='*62}", flush=True)
    print(f"  Benchmark complete", flush=True)
    print(f"  Configurations: {total}", flush=True)
    print(f"  CLEAN_PASS:           {counts['CLEAN_PASS']:3d}", flush=True)
    print(f"  RECOVERED_VIA_REROUTE:{counts['RECOVERED_VIA_REROUTE']:3d}", flush=True)
    print(f"  CHANNEL_FAILURE:      {counts['CHANNEL_FAILURE']:3d}", flush=True)
    print(f"  SESSION_ABORTED:      {counts['SESSION_ABORTED']:3d}  "
          f"(injected_qber > 0.11 after {2} retries)", flush=True)
    print(f"  RECONCILIATION_INCOMPLETE:{counts['RECONCILIATION_INCOMPLETE']:3d}  "
          f"(even-count errors in block — single-pass limitation)", flush=True)
    print(f"  KEY_MISMATCH_ERROR:   {counts['KEY_MISMATCH_ERROR']:3d}  "
          f"(should be 0 now — residual if reconciliation logic is bypassed)", flush=True)
    print(f"  TIMEOUT:              {counts['TIMEOUT']:3d}  "
          f"(hung >60s — unexpected; socket fix should eliminate these)", flush=True)
    print(f"  ERROR:                {counts['ERROR']:3d}", flush=True)
    print(f"  Records written:      {successful}", flush=True)
    print(f"  Elapsed:              {total_elapsed:.1f}s", flush=True)
    print(f"  Log: {log_path.relative_to(_PROJECT_ROOT)}", flush=True)
    print(f"{'='*62}\n", flush=True)


if __name__ == "__main__":
    main()
