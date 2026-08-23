"""
Reusable end-to-end session runner
==================================

Extracted from scripts/run_benchmark.py::run_one_transfer() (which itself is a
hardened superset of tests/test_end_to_end.py::_run_full_pipeline()) so that a
full hybrid quantum-classical transfer can be invoked programmatically:

    from quantum_demo.pipeline import run_session
    result = run_session({"protocol": "BB84", "injected_qber": 0.13})

Why the benchmark version and not the test version:
the test orchestration derives AES keys directly from the raw QKD session key,
so any noise above ~0.05 crashes with an AES InvalidTag or hangs Alice's recv()
forever. The benchmark orchestration adds Phase 9 reconciliation, explicit
channel closes on every error path (the documented hang fix), and clean
SESSION_ABORTED / RECON_INCOMPLETE outcome mapping -- all required for live
noise-sweep demos.

Outcome vocabulary returned in SessionResult.outcome:
    "CLEAN_PASS" | "RECOVERED_VIA_REROUTE" | "CHANNEL_FAILURE"   (echo phase)
    "SESSION_ABORTED"                                            (QBER > 0.11)
    "RECON_INCOMPLETE"                                           (Phase 9 failed)
    "ERROR" | "TIMEOUT"                                          (orchestrator)

Log capture: every module logger is created via metrics/logger.get_logger(),
which attaches a stdout handler AND sets propagate=False on each NAMED logger.
A root-logger handler therefore sees nothing. We attach an in-memory handler
directly to every logger registered in logging.root.manager.loggerDict.
"""

from __future__ import annotations

import logging
import pathlib
import socket
import sys
import threading
import time
import uuid

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from classical.aes_channel import derive_key
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

from quantum_demo.models import SessionResult, SplitInfo

_DEFAULT_TIMEOUT_S = 45.0


# ---------------------------------------------------------------------------
# Log capture
# ---------------------------------------------------------------------------

class _CaptureHandler(logging.Handler):
    """Appends each formatted record to a list; optionally fires a callback."""

    def __init__(self, sink: list, callback=None):
        super().__init__()
        self._sink = sink
        self._callback = callback
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            return
        self._sink.append(line)
        if self._callback is not None:
            try:
                self._callback(line)
            except Exception:
                pass


def _attach_capture(handler: logging.Handler) -> list:
    """
    Attach handler to every named logger.

    get_logger() sets propagate=False on each module logger, so attaching to
    the root logger alone would capture nothing. loggerDict holds every name
    get_logger() has ever created; getLogger(name) materializes placeholders.
    Returns the list of loggers we touched so they can be restored.
    """
    touched = []
    for name in list(logging.root.manager.loggerDict) + [None]:
        lg = logging.getLogger(name) if name else logging.getLogger()
        lg.addHandler(handler)
        touched.append(lg)
    return touched


def _detach_capture(handler: logging.Handler, touched: list) -> None:
    for lg in touched:
        try:
            lg.removeHandler(handler)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers (mirrors scripts/run_benchmark.py)
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _normalize_config(config: dict) -> dict:
    cfg = dict(config)
    payload_size = int(cfg.get("payload_size_bytes", 64))
    cfg.setdefault("host", "127.0.0.1")
    cfg.setdefault("protocol", "BB84")
    cfg.setdefault("security_level", "medium")
    cfg.setdefault("injected_qber", 0.0)
    cfg.setdefault("n_qubits", 200)
    cfg.setdefault("n_pairs_per_setting", 50)
    # 2x the maximum possible quantum demand (see run_benchmark.py main loop)
    cfg.setdefault("available_ebits", payload_size * 4 * 2)
    cfg.setdefault("server_port", _free_port())
    cfg.setdefault("data_port", _free_port())
    cfg.setdefault("quantum_port", _free_port())
    return cfg


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_session(
    config: dict,
    payload: bytes | None = None,
    fault_cfg: dict | None = None,
    metrics_log_path: str | pathlib.Path | None = None,
    on_log_line=None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> SessionResult:
    """
    Run one complete hybrid transfer: server startup -> registration ->
    QKD (BB84/E91) -> reconciliation -> split decision -> concurrent
    dual-channel transmission -> echo validation -> optional metrics record.

    Parameters
    ----------
    config : dict
        Optional keys: protocol ("BB84"|"E91"), security_level
        ("low"|"medium"|"high"), injected_qber (float), payload_size_bytes
        (int), n_qubits (int), available_ebits (int), host/ports. Any missing
        key gets the system default; ports are auto-assigned if absent.
    payload : bytes | None
        Explicit payload bytes. Defaults to the benchmark's deterministic
        pattern bytes(i % 256 for i in range(payload_size_bytes)).
    fault_cfg : dict | None
        Classical-channel fault injector config, e.g.
        {"fault_injection": {"enabled": True, "bit_error_probability": 0.05}}.
        None / disabled by default.
    metrics_log_path : path | None
        When given, appends one schema-conformant JSONL record (same format as
        the benchmark) via MetricsCollector.
    on_log_line : callable(str) | None
        Invoked with each structured log line AS IT IS EMITTED (used by the
        WebSocket stream). Also collected into SessionResult.log_lines either way.
    timeout_s : float
        Per-thread join timeout; exceeding it yields outcome="TIMEOUT".

    Returns
    -------
    SessionResult
    """
    cfg = _normalize_config(config)
    if payload is None:
        psz = int(cfg["payload_size_bytes"])
        payload = bytes(i % 256 for i in range(psz))

    log_lines: list = []
    capture = _CaptureHandler(log_lines, on_log_line)
    touched = _attach_capture(capture)
    try:
        return _run(cfg, payload, fault_cfg, metrics_log_path,
                    timeout_s, log_lines)
    finally:
        _detach_capture(capture, touched)


def _run(cfg, payload, fault_cfg, metrics_log_path, timeout_s, log_lines):
    from classical.fault_injector import FaultInjector
    injector = FaultInjector(fault_cfg or {"fault_injection": {"enabled": False}})

    protocol = cfg["protocol"]
    server = EbitServer(cfg)
    server.start()
    server.ready.wait(timeout=5)

    node_a = Node("A", cfg)
    node_b = Node("B", cfg)

    errors: dict = {}
    b_registered = threading.Event()
    b_channels_ready = threading.Event()

    node_a_ref: list = [None]
    bob_payload_h: list = [None]
    echo_result_h: list = [None]
    qkd_elapsed_h: list = [0.0]
    xfer_elapsed_h: list = [0.0]
    decision_h: list = [None]
    split_h: list = [None]
    session_aborted_h: list = [None]     # str reason or None
    recon_incomplete_h: list = [False]

    def run_b():
        c_dc = None
        q_dc = None
        try:
            node_b.connect(); node_b.register()
            b_registered.set()
            node_b.wait_for_connection()
            node_b.run_qkd()

            c_dc = DataChannel(cfg["host"], cfg["data_port"])
            q_dc = DataChannel(cfg["host"], cfg["quantum_port"])
            c_dc.listen(); q_dc.listen()
            b_channels_ready.set()
            c_dc.accept(); q_dc.accept()

            qber = max(node_b.session.qber, 1e-4)
            recon = reconcile_bob(node_b.session.key, qber, c_dc)
            if not recon.reconciled_bits:
                raise ReconciliationIncompleteError(
                    "No bits remain after privacy amplification")
            aes_key = derive_key(recon.reconciled_bits)
            verify_key_bob(c_dc, aes_key)

            bob_payload_h[0] = bob_transfer(node_b, c_dc, q_dc, aes_key)
            c_dc.close(); q_dc.close()
        except SessionAbortedError as exc:
            session_aborted_h[0] = str(exc)
            b_registered.set(); b_channels_ready.set()
        except ReconciliationIncompleteError as exc:
            errors.setdefault("B", exc)
            recon_incomplete_h[0] = True
            b_registered.set(); b_channels_ready.set()
            _close_quietly(c_dc); _close_quietly(q_dc)
        except Exception as exc:
            errors.setdefault("B", exc)
            b_registered.set(); b_channels_ready.set()
            # Explicit close so Alice's blocking recv() unblocks immediately
            # instead of hanging until timeout (critical InvalidTag-hang fix).
            _close_quietly(c_dc); _close_quietly(q_dc)
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

            c_dc = DataChannel(cfg["host"], cfg["data_port"])
            q_dc = DataChannel(cfg["host"], cfg["quantum_port"])
            c_dc.connect(); q_dc.connect()

            qber = max(node_a.session.qber, 1e-4)
            recon = reconcile_alice(node_a.session.key, qber, c_dc)
            if not recon.reconciled_bits:
                raise ReconciliationIncompleteError(
                    "No bits remain after privacy amplification")
            aes_key = derive_key(recon.reconciled_bits)
            verify_key_alice(c_dc, aes_key)

            decision = compute_split(
                security_level=cfg["security_level"],
                payload_size_bytes=len(payload),
                available_ebits=cfg["available_ebits"],
                qber=node_a.session.qber,
            )
            decision_h[0] = decision
            split_h[0] = split_payload(payload, decision)

            classical_seg = split_h[0].classical_segment
            if injector.enabled and len(classical_seg) > 0:
                injector.process_plaintext(classical_seg)

            t1 = time.monotonic()
            result = alice_transfer(
                node_a, payload, decision, c_dc, q_dc, aes_key,
                cfg["available_ebits"], node_a.session.qber,
            )
            xfer_elapsed_h[0] = time.monotonic() - t1
            echo_result_h[0] = result
            node_a_ref[0] = node_a

            c_dc.close(); q_dc.close()
        except SessionAbortedError as exc:
            session_aborted_h[0] = str(exc)
        except ReconciliationIncompleteError:
            recon_incomplete_h[0] = True
        except Exception as exc:
            errors.setdefault("A", exc)
        finally:
            node_a.close()

    tb = threading.Thread(target=run_b, daemon=True)
    ta = threading.Thread(target=run_a, daemon=True)
    tb.start(); ta.start()
    tb.join(timeout=timeout_s); ta.join(timeout=timeout_s)
    server.stop()

    # ----- map outcomes -----

    aborted = session_aborted_h[0] is not None or any(
        isinstance(e, SessionAbortedError) for e in errors.values())
    timed_out = tb.is_alive() or ta.is_alive()

    qber = None
    chsh = None
    skr = None
    if node_a_ref[0] is not None and getattr(node_a_ref[0], "session", None):
        qber = node_a_ref[0].session.qber
        chsh = node_a_ref[0].session.chsh
        if qkd_elapsed_h[0] > 0 and node_a_ref[0].session.key:
            skr = len(node_a_ref[0].session.key) / qkd_elapsed_h[0]

    split_info = None
    if decision_h[0] is not None:
        split_info = SplitInfo(
            quantum_fraction=decision_h[0].quantum_fraction,
            classical_fraction=decision_h[0].classical_fraction,
            reason=decision_h[0].reason,
        )

    throughput = None
    latency = None
    echo_outcome = None
    if echo_result_h[0] is not None:
        er = echo_result_h[0]
        if xfer_elapsed_h[0] > 0:
            throughput = len(payload) / xfer_elapsed_h[0]
            latency = xfer_elapsed_h[0]
        if er.success and not er.recovered:
            echo_outcome = "CLEAN_PASS"
        elif er.success and er.recovered:
            echo_outcome = "RECOVERED_VIA_REROUTE"
        else:
            echo_outcome = "CHANNEL_FAILURE"

    if timed_out:
        outcome = "TIMEOUT"
        abort_reason = f"thread join timeout after {timeout_s}s"
    elif aborted:
        outcome = "SESSION_ABORTED"
        abort_reason = session_aborted_h[0] or next(
            (str(e) for e in errors.values()
             if isinstance(e, SessionAbortedError)), "QBER_EXCEEDED")
    elif recon_incomplete_h[0]:
        outcome = "RECON_INCOMPLETE"
        abort_reason = next((str(e) for e in errors.values()
                             if isinstance(e, ReconciliationIncompleteError)),
                            "key reconciliation failed")
    elif errors:
        outcome = "ERROR"
        abort_reason = "; ".join(f"{k}: {v!r}" for k, v in errors.items())
    else:
        outcome = echo_outcome or "CHANNEL_FAILURE"
        abort_reason = None

    # ----- optional metrics record (same JSONL schema as the benchmark) -----
    if metrics_log_path is not None and echo_result_h[0] is not None \
            and split_h[0] is not None:
        try:
            collector = MetricsCollector(metrics_log_path)
            collector.record_transfer(
                session_id=str(uuid.uuid4()),
                protocol=cfg["protocol"],
                qber=qber or 0.0,
                chsh=chsh,
                qkd_key_bits=len(node_a_ref[0].session.key) if node_a_ref[0] else 0,
                qkd_elapsed_s=qkd_elapsed_h[0],
                decision=decision_h[0],
                payload_bytes=len(payload),
                quantum_bytes=split_h[0].quantum_len,
                classical_bytes=split_h[0].classical_len,
                transfer_elapsed_s=xfer_elapsed_h[0],
                echo_result=echo_result_h[0],
                fault_injector=injector if injector.enabled else None,
            )
        except Exception:
            pass  # metrics must never take down the demo

    return SessionResult(
        outcome=outcome,
        protocol=cfg["protocol"],
        qber=qber,
        chsh=chsh,
        skr=skr,
        split=split_info,
        throughput_bps=throughput,
        latency_s=latency,
        abort_reason=abort_reason,
        log_lines=log_lines,
        bob_payload=bob_payload_h[0],
    )


def _close_quietly(dc) -> None:
    if dc is None:
        return
    try:
        dc.close()
    except Exception:
        pass
