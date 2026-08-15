"""
Metrics collector — Phase 7a

Accepts already-computed values from the caller (Node A's orchestration
layer) and appends one structured JSON record per completed transfer to
metrics/logs/transfers.jsonl.

Design: purely additive / observational — this module does NOT modify any
Phase 1-6 module.  The caller wraps timing around existing calls and passes
the results in.  The collector is a simple data aggregator + writer.

Record schema (one JSON object per line in transfers.jsonl):
    schema_version        : "1.0"
    session_id            : str    — UUID per QKD session
    transfer_id           : str    — UUID per payload transfer
    timestamp_utc         : str    — ISO-8601

    -- QKD metrics --
    protocol              : "BB84" | "E91"
    qber                  : float
    chsh                  : float | null   (E91 only)
    qkd_key_bits          : int
    qkd_elapsed_s         : float
    skr_bits_per_second   : float   = qkd_key_bits / qkd_elapsed_s

    -- Split decision --
    split_reason          : str    (one of split_controller REASON_* tokens)
    quantum_fraction      : float
    classical_fraction    : float

    -- Payload sizes --
    payload_bytes         : int
    quantum_bytes         : int
    classical_bytes       : int

    -- Transfer timing --
    transfer_elapsed_s    : float   (start of alice_transfer → echo confirmed)
    throughput_bytes_per_s: float   = payload_bytes / transfer_elapsed_s
    latency_s             : float   same as transfer_elapsed_s (end-to-end)

    -- Echo outcome --
    echo_outcome          : "CLEAN_PASS" | "RECOVERED_VIA_REROUTE" | "CHANNEL_FAILURE"
    echo_recovered        : bool
    mismatch_source       : "quantum" | "classical" | "both" | null
    echo_diagnosis        : "QUANTUM_CHANNEL_ISSUE" | "POSSIBLE_EAVESDROPPER" | null
    retransmit_bytes      : int

    -- Fault injection (null when injector off; all labeled _simulated) --
    fault_injection_enabled    : bool
    ber_simulated              : float | null
    snr_db_simulated           : float | null   (BSC model proxy, not physical)
    plr_simulated              : float | null
    bits_injected_errors       : int
    packets_simulated_dropped  : int

SKR formula:
    skr_bits_per_second = qkd_key_bits / qkd_elapsed_s
    The QKD timing is measured by the caller (time.monotonic() before/after
    node.run_qkd()), not inside node.py itself.

Throughput:
    Total payload bytes successfully transferred / wall-clock transfer time.
    Includes any Phase 6 reroute overhead (it's end-to-end application-layer
    throughput, not channel capacity).
"""

from __future__ import annotations

import json
import math
import pathlib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

__all__ = ["TransferRecord", "MetricsCollector"]

_DEFAULT_LOG_PATH = pathlib.Path(__file__).parent / "logs" / "transfers.jsonl"

SCHEMA_VERSION = "1.0"


@dataclass
class TransferRecord:
    """One record per completed transfer.  Written as a single JSON line."""

    # Identity
    schema_version:  str = SCHEMA_VERSION
    session_id:      str = field(default_factory=lambda: str(uuid.uuid4()))
    transfer_id:     str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp_utc:   str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    )

    # QKD
    protocol:             str   = "BB84"
    qber:                 float = 0.0
    chsh:                 float | None = None
    qkd_key_bits:         int   = 0
    qkd_elapsed_s:        float = 0.0
    skr_bits_per_second:  float = 0.0

    # Split
    split_reason:         str   = ""
    quantum_fraction:     float = 0.0
    classical_fraction:   float = 1.0

    # Payload
    payload_bytes:        int   = 0
    quantum_bytes:        int   = 0
    classical_bytes:      int   = 0

    # Timing
    transfer_elapsed_s:        float = 0.0
    throughput_bytes_per_s:    float = 0.0
    latency_s:                 float = 0.0

    # Echo
    echo_outcome:    str        = "CLEAN_PASS"
    echo_recovered:  bool       = False
    mismatch_source: str | None = None
    echo_diagnosis:  str | None = None
    retransmit_bytes: int       = 0

    # Fault injection (null when off)
    fault_injection_enabled:   bool        = False
    ber_simulated:             float | None = None
    snr_db_simulated:          float | None = None
    plr_simulated:             float | None = None
    bits_injected_errors:      int          = 0
    packets_simulated_dropped: int          = 0


class MetricsCollector:
    """
    Appends one JSON record per transfer to a .jsonl log file.

    Usage (Alice's orchestration):

        collector = MetricsCollector()          # uses default log path
        session_id = str(uuid.uuid4())

        qkd_start = time.monotonic()
        node_a.run_qkd()
        qkd_elapsed = time.monotonic() - qkd_start

        transfer_start = time.monotonic()
        echo_result = alice_transfer(...)
        transfer_elapsed = time.monotonic() - transfer_start

        collector.record_transfer(
            session_id       = session_id,
            protocol         = config["protocol"],
            qber             = node_a.session.qber,
            chsh             = node_a.session.chsh,
            qkd_key_bits     = len(node_a.session.key),
            qkd_elapsed_s    = qkd_elapsed,
            decision         = decision,
            payload_bytes    = len(payload),
            quantum_bytes    = split.quantum_len,
            classical_bytes  = split.classical_len,
            transfer_elapsed_s = transfer_elapsed,
            echo_result      = echo_result,
            fault_injector   = injector,   # None if not used
        )
    """

    def __init__(self, log_path: str | pathlib.Path | None = None) -> None:
        self.log_path = pathlib.Path(log_path) if log_path else _DEFAULT_LOG_PATH

    def record_transfer(
        self,
        *,
        session_id:        str,
        protocol:          str,
        qber:              float,
        chsh:              float | None,
        qkd_key_bits:      int,
        qkd_elapsed_s:     float,
        decision,                          # SplitDecision
        payload_bytes:     int,
        quantum_bytes:     int,
        classical_bytes:   int,
        transfer_elapsed_s: float,
        echo_result,                       # EchoResult
        fault_injector=None,               # FaultInjector | None
        transfer_id:       str | None = None,
    ) -> TransferRecord:
        """
        Build and persist one TransferRecord.

        Returns the record so the caller can inspect it (useful in tests).
        """
        skr = qkd_key_bits / qkd_elapsed_s if qkd_elapsed_s > 0 else 0.0
        throughput = payload_bytes / transfer_elapsed_s if transfer_elapsed_s > 0 else 0.0

        # Echo outcome string
        if echo_result.success and not echo_result.recovered:
            outcome = "CLEAN_PASS"
        elif echo_result.success and echo_result.recovered:
            outcome = "RECOVERED_VIA_REROUTE"
        else:
            outcome = "CHANNEL_FAILURE"

        # Fault injector fields
        fi_enabled    = fault_injector is not None and fault_injector.enabled
        ber_sim       = fault_injector.ber    if fi_enabled else None
        snr_db_sim    = fault_injector.snr_db if fi_enabled else None
        plr_sim       = fault_injector.plr    if fi_enabled else None
        err_bits      = fault_injector.error_bits       if fi_enabled else 0
        dropped       = fault_injector.dropped_packets  if fi_enabled else 0

        rec = TransferRecord(
            session_id             = session_id,
            transfer_id            = transfer_id or str(uuid.uuid4()),
            protocol               = protocol,
            qber                   = qber,
            chsh                   = chsh,
            qkd_key_bits           = qkd_key_bits,
            qkd_elapsed_s          = round(qkd_elapsed_s, 6),
            skr_bits_per_second    = round(skr, 2),
            split_reason           = decision.reason,
            quantum_fraction       = decision.quantum_fraction,
            classical_fraction     = decision.classical_fraction,
            payload_bytes          = payload_bytes,
            quantum_bytes          = quantum_bytes,
            classical_bytes        = classical_bytes,
            transfer_elapsed_s     = round(transfer_elapsed_s, 6),
            throughput_bytes_per_s = round(throughput, 2),
            latency_s              = round(transfer_elapsed_s, 6),
            echo_outcome           = outcome,
            echo_recovered         = echo_result.recovered,
            mismatch_source        = echo_result.mismatch_source,
            echo_diagnosis         = echo_result.echo_diagnosis if hasattr(echo_result, "echo_diagnosis") else echo_result.diagnosis,
            retransmit_bytes       = echo_result.retransmit_bytes,
            fault_injection_enabled    = fi_enabled,
            ber_simulated              = round(ber_sim, 8) if ber_sim is not None else None,
            snr_db_simulated           = snr_db_sim,
            plr_simulated              = round(plr_sim, 6) if plr_sim is not None else None,
            bits_injected_errors       = err_bits,
            packets_simulated_dropped  = dropped,
        )

        self._write(rec)
        return rec

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _write(self, rec: TransferRecord) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(rec), default=_json_default) + "\n"
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(line)


def _json_default(obj):
    """JSON serializer for non-standard types (e.g. float('inf'), None)."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None   # JSON has no Infinity/NaN — collapse to null
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
