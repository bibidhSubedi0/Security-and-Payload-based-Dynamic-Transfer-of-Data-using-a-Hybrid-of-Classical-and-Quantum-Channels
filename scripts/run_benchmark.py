#!/usr/bin/env python3
r"""
End-to-End Benchmark Sweep (Phase 8 Part C)
===========================================

Runs the fully assembled hybrid quantum-classical transfer pipeline
(QKD, reconciliation, key derivation, dynamic split, echo-validated
transfer) across a fixed grid of operating conditions and logs one
metrics record per configuration to a timestamped .jsonl file.

Why
---
Unit tests prove each phase works in isolation; this script proves the
assembled system works together, and produces the empirical dataset
(success rate vs noise, QBER, SKR, throughput, latency, split ratios) that
scripts/generate_dashboard.py renders into figures + dashboard.html.

--------------------------
Per-Configuration Pipeline
--------------------------
Each of the 54 configurations spins up an isolated loopback deployment
(fresh free ports each iteration, sequential execution):

1. Start EbitServer; Node A and Node B register and establish a connection.
2. Both sides run BB84 QKD (n_qubits=200) → shared sifted key + measured
   QBER. Measured QBER > 0.11 (abort threshold) → SessionAbortedError.
3. Single-pass Cascade reconciliation corrects residual bit errors, then
   HKDF-SHA256 derives the AES-256-GCM session key; an explicit key
   verification round runs before any payload moves. If privacy
   amplification leaves no bits → ReconciliationIncompleteError.
4. Alice computes the dynamic split (split_controller.compute_split) from
   security level, payload size, available ebits, and measured QBER.
5. Optional fault injection corrupts the classical segment plaintext
   before transmission (models channel degradation).
6. Echo-validated transfer (transmission.echo_validation): quantum segment
   via superdense coding over the quantum channel, classical segment
   AES-GCM encrypted over the data channel; Bob echoes a digest back for
   end-to-end validation.

----
Grid
----
  security_level  : ["low", "medium", "high"]           (3 values)
  payload_size_B  : [16, 128, 512]                      (3 values, small/medium/large)
  injected_qber   : [0.0, 0.05, 0.12]                   (3 values)
                    0.0  → noiseless channel
                    0.05 → moderate degradation (QBER < 0.11 → transfers proceed)
                    0.12 → above abort threshold (QBER > 0.11 → SESSION_ABORTED)
  fault_injection : [off, BER=0.05]                     (2 values)

Total: 3 * 3 * 3 * 2 = 54 configurations.
NOTE: noise=0.12 configs never produce transfer records: they are aborted
before any payload moves, so only their failure records appear in the log.
Consumers (generate_dashboard.py) reconstruct this row via grid arithmetic.

-------------
Outcome Codes
-------------
  CLEAN_PASS                 : echo validated on first attempt
  RECOVERED_VIA_REROUTE      : echo validated after in-transfer recovery
  CHANNEL_FAILURE            : echo digest mismatch (integrity lost)
  SESSION_ABORTED            : QBER above 0.11 → no key derived
  RECONCILIATION_INCOMPLETE  : nothing left after privacy amplification
  KEY_MISMATCH_ERROR         : InvalidTag raised by AES-GCM; keys diverged
                               despite reconciliation
  TIMEOUT                    : threads still alive after 60s (unexpected)
  ERROR                      : anything else

-------
Output
-------
  metrics/logs/benchmark_<YYYYMMDD_HHMMSS>.jsonl: one JSON line per config.
  Failures are logged too (record_failure), not just successes: earlier
  versions logged only completed transfers, which made the success rate read
  100% by construction (survivorship bias). Schema ≥ 1.1 records carry the
  reconciliation fields bits_corrected / bits_sacrificed.

-----------
Integration
-----------
Downstream consumer: scripts/generate_dashboard.py reads every
benchmark_*.jsonl in metrics/logs/, renders matplotlib PNGs plus the
interactive Plotly page metrics/dashboard.html, and mirrors it to
demo-frontend/public/dashboard.html for the React frontend.

Speed notes
-----------
n_qubits=200 matches the system default used in Phase 1-7 tests.
n_pairs_per_setting=50 (E91 CHSH verification runs, not used here; BB84 only).
Sequential execution keeps memory stable and avoids port contention;
each configuration is bounded by a 60s timeout.

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

# Qubit count for every BB84 session in the sweep.
# 200 matches the system default exercised by the Phase 1-7 tests, keeping
# benchmark statistics comparable with the unit-test figures (~100 sifted
# bits, ~75 check bits at the default check fraction of 0.75).
N_QUBITS = 200

# Split-policy axis: exercises every branch of compute_split().
# low -> ~25% quantum share, medium -> ~50%, high -> ~75% of the payload.
SECURITY_LEVELS = ["low", "medium", "high"]

# Payload axis: representative message sizes spanning an order of magnitude.
PAYLOAD_SIZES = [
    (16,  "small"),   # typical short control message
    (128, "medium"),  # small data packet
    (512, "large"),   # moderate payload; max quantum segment = 384 B = 1536 ebits
]

# Noise axis: covers both sides of the 0.11 BB84 abort threshold.
#   0.00 noiseless       : baseline, measured QBER should be exactly 0.
#   0.05 moderate        : below threshold, transfers proceed end to end.
#   0.12 above_threshold : deliberately past BB84_QBER_THRESHOLD (0.11),
#                          sessions must abort before any payload moves.
NOISE_LEVELS = [
    (0.00, "noiseless"),
    (0.05, "moderate"),
    (0.12, "above_threshold"),
]

# Classical-channel fault axis: off vs 5% bit-error rate on the classical
# segment plaintext. Packet loss stays at 0 because the reroute-recovery
# path under test reacts to corrupted bits, not to dropped packets.
FAULT_CONFIGS = [
    ({"fault_injection": {"enabled": False}},                        "off"),
    ({"fault_injection": {"enabled": True, "bit_error_probability": 0.05,
                          "packet_loss_probability": 0.0}},           "BER5%"),
]

# -------
# Total configs = 3 × 3 × 3 × 2 = 54
# -------


def _free_port() -> int:
    r"""
    Obtain an unused loopback port from the OS.

    -------
    Returns
    -------
    int
        A port number that was free at call time.

    -----
    Notes
    -----
    Binding to port 0 lets the kernel pick an ephemeral port, so concurrent
    benchmark runs never fight over fixed port numbers. The port could in
    principle be re-assigned between this call and the caller's bind, but
    each configuration binds immediately, making collisions vanishingly rare.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_config(server_port: int, data_port: int, quantum_port: int,
                 security_level: str, available_ebits: int,
                 noise: float) -> dict:
    r"""
    Assemble the shared config dict consumed by EbitServer and both Nodes.

    ----------
    Parameters
    ----------
    server_port : int
        Loopback port for the EbitServer control plane (ebit distribution).
    data_port : int
        Loopback port for the AES-GCM encrypted classical data channel.
    quantum_port : int
        Loopback port for the superdense-coding quantum channel stream.
    security_level : str
        "low" | "medium" | "high"; drives the compute_split() policy.
    available_ebits : int
        Entanglement budget advertised to the split controller; the sweep
        sets this to 2x the maximum possible quantum demand of the payload.
    noise : float
        injected_qber passed through to the QKD simulation.

    -------
    Returns
    -------
    dict
        Config in the shape expected by EbitServer(config) / Node(name, config):
        protocol is pinned to "BB84" and n_pairs_per_setting=50 is carried
        for interface compatibility even though BB84 ignores it.
    """
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
    r"""
    Execute one complete transfer pipeline in an isolated loopback deployment.

    Spins up EbitServer + Node A + Node B, runs QKD, reconciliation, key
    verification, split computation and the echo-validated transfer, then
    tears everything down. This is the unit of work main() iterates over
    the grid.

    ----------
    Parameters
    ----------
    config : dict
        Assembled by _make_config(); shared verbatim by server and nodes.
    payload : bytes
        Plaintext message for Alice to transfer.
    fault_cfg : dict
        FaultInjector configuration for the classical segment
        (see FAULT_CONFIGS).
    timeout_s : float, default 60.0
        Wall-clock budget for both worker threads before the run is
        declared TIMED OUT.

    -------
    Returns
    -------
    dict
        On completion:
          ok               : True when no error, abort or incomplete reconciliation
          aborted          : True when QKD raised SessionAbortedError (QBER > 0.11)
          recon_incomplete : True when privacy amplification left no bits
          errors           : {"A"/"B": exception} from whichever side failed
          node_a           : Node A instance (None if it never got that far);
                             carries session (qber, key, chsh) and timings
          echo_result      : EchoValidationResult from transmission.echo_validation
          qkd_elapsed_s    : wall time of the BB84 exchange
          xfer_elapsed_s   : wall time of the payload transfer itself
          decision, split  : compute_split() output and split_payload() output
          recon_bob        : Bob-side Cascade result (source of bits_corrected /
                             bits_sacrificed in the metrics log)
          injector         : the FaultInjector instance actually used
        On thread hang:
          timed_out=True plus tb_alive/ta_alive flags; all other keys absent.

    -----
    Notes
    -----
    Alice and Bob each run on their own thread because both sides block on
    socket I/O against each other; the threads communicate readiness through
    threading.Event objects and share results through single-element list
    holders (closure cells are read-only). Bob explicitly closes his data
    channels on any failure so Alice's blocking recv() unblocks at once
    instead of hanging until the per-config timeout.
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
    recon_bob_h:         list = [None]
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
            recon_bob_h[0] = recon   # Bob's view: bits_corrected is meaningful
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
        "recon_bob":        recon_bob_h[0],
        "injector":         injector,
    }


def main() -> None:
    r"""
    Orchestrate the full benchmark sweep.

    ----------
    Workflow
    ----------
    1. Open a MetricsCollector on metrics/logs/benchmark_<timestamp>.jsonl;
       one session_id (uuid4) groups all records of this sweep.
    2. Expand the grid (54 configs) and iterate sequentially.
    3. Per config: build payload (deterministic byte pattern i % 256),
       provision ebit budget at 2x the max quantum demand so ebit
       scarcity never distorts the split-policy axis, run
       run_one_transfer(), classify the outcome, and write either a
       record_transfer() or record_failure() row.
    4. Print a per-config progress line plus a final summary with outcome
       counters, records written and total elapsed time.

    -----
    Notes
    -----
    Outcome classification order matters: aborts and reconciliation
    failures are checked before generic errors because they carry
    protocol semantics, not bugs. KEY_MISMATCH_ERROR is detected by
    exception type name (InvalidTag from cryptography) rather than an
    import, keeping this script decoupled from the crypto backend.
    """
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
    print(f"  Phase 8 Benchmark sweep  ({total} configurations)", flush=True)
    print(f"  Log: {log_path.relative_to(_PROJECT_ROOT)}", flush=True)
    print(f"{'='*62}\n", flush=True)

    counts = {"CLEAN_PASS": 0, "RECOVERED_VIA_REROUTE": 0,
              "CHANNEL_FAILURE": 0, "SESSION_ABORTED": 0,
              "RECONCILIATION_INCOMPLETE": 0,
              "KEY_MISMATCH_ERROR": 0, "ERROR": 0, "TIMEOUT": 0}
    sweep_start = time.monotonic()
    records_written = 0

    def log_failure(outcome: str) -> None:
        """
        Persist one failure record so failed transfers appear in the .jsonl
        too (previously they only incremented console counters, giving the
        log survivorship bias: success rate read as 100% by construction).
        Fields unknown at the failure point stay at neutral defaults.
        """
        nonlocal records_written
        try:
            collector.record_failure(
                session_id         = session_id,
                outcome            = outcome,
                payload_bytes      = psz,
                decision           = r.get("decision"),
                quantum_bytes      = r["split"].quantum_len if r.get("split") else 0,
                classical_bytes    = r["split"].classical_len if r.get("split") else 0,
                transfer_elapsed_s = r.get("xfer_elapsed_s", 0.0),
                fault_injector     = r.get("injector"),
            )
            records_written += 1
        except Exception as exc:
            print(f"  [WARNING] MetricsCollector write failed: {exc}", flush=True)

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
            log_failure("TIMEOUT")
            continue

        if r["aborted"]:
            outcome = "SESSION_ABORTED"
            counts[outcome] += 1
            print(f"  → {outcome:<22s} ({elapsed:.2f}s)", flush=True)
            log_failure(outcome)
            continue

        if r["recon_incomplete"]:
            outcome = "RECONCILIATION_INCOMPLETE"
            counts[outcome] += 1
            print(f"  → {outcome:<22s} ({elapsed:.2f}s)", flush=True)
            log_failure(outcome)
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
            log_failure(outcome)
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
                bits_corrected    = r["recon_bob"].bits_corrected if r["recon_bob"] else None,
                bits_sacrificed   = r["recon_bob"].bits_sacrificed if r["recon_bob"] else None,
            )
            records_written += 1
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
    print(f"\n{'='*62}", flush=True)
    print(f"  Benchmark complete", flush=True)
    print(f"  Configurations: {total}", flush=True)
    print(f"  CLEAN_PASS:           {counts['CLEAN_PASS']:3d}", flush=True)
    print(f"  RECOVERED_VIA_REROUTE:{counts['RECOVERED_VIA_REROUTE']:3d}", flush=True)
    print(f"  CHANNEL_FAILURE:      {counts['CHANNEL_FAILURE']:3d}", flush=True)
    print(f"  SESSION_ABORTED:      {counts['SESSION_ABORTED']:3d}  "
          f"(injected_qber > 0.11 after {2} retries)", flush=True)
    print(f"  RECONCILIATION_INCOMPLETE:{counts['RECONCILIATION_INCOMPLETE']:3d}  "
          f"(even-count errors in block; single-pass limitation)", flush=True)
    print(f"  KEY_MISMATCH_ERROR:   {counts['KEY_MISMATCH_ERROR']:3d}  "
          f"(should be 0 now; residual if reconciliation logic is bypassed)", flush=True)
    print(f"  TIMEOUT:              {counts['TIMEOUT']:3d}  "
          f"(hung >60s, unexpected; socket fix should eliminate these)", flush=True)
    print(f"  ERROR:                {counts['ERROR']:3d}", flush=True)
    print(f"  Records written:      {records_written:3d}  "
          f"(successes + failures)", flush=True)
    print(f"  Elapsed:              {total_elapsed:.1f}s", flush=True)
    print(f"  Log: {log_path.relative_to(_PROJECT_ROOT)}", flush=True)
    print(f"{'='*62}\n", flush=True)


if __name__ == "__main__":
    main()
