r"""
Metrics Collector (Phase 7a)
============================

Accepts already-computed values from the caller (Node A's orchestration layer)
and appends one structured JSON record per completed transfer to
metrics/logs/transfers.jsonl.

----------------
Design Contract
----------------
Purely additive and observational: this module does NOT modify any Phase 1-6
module. ALL timing lives in the caller, which wraps time.monotonic() around
existing calls (node.run_qkd(), alice_transfer()) and passes the results in.
The collector is a data aggregator plus an append-only writer; it holds no
protocol state and influences no protocol decisions.

Consequence: a collector crash must never take down a transfer. The benchmark
script wraps record_transfer() in try/except for exactly this reason.

-------------------------
Record Schema (JSONL)
-------------------------
One JSON object per line in transfers.jsonl. Field groups, types, UNITS:

Identity:
    schema_version        : str    "1.1" (bump on any schema change)
    session_id            : str    UUID4, one per QKD session
    transfer_id           : str    UUID4, one per payload transfer
    timestamp_utc         : str    ISO-8601, UTC, microsecond resolution

QKD:
    protocol              : str    "BB84" | "E91"
    qber                  : float  dimensionless ratio in [0, 1]
    chsh                  : float | null   E91 only; null under BB84
    qkd_key_bits          : int    UNIT: bits (post reconciliation key length)
    qkd_elapsed_s         : float  UNIT: seconds, 6 dp (caller-measured)
    skr_bits_per_second   : float  UNIT: bits/s, derived (formula below)

Split decision:
    split_reason          : str    one of split_controller REASON_* tokens
    quantum_fraction      : float  dimensionless, in [0, 1]
    classical_fraction    : float  dimensionless, in [0, 1]; sums to 1

Payload sizes:
    payload_bytes         : int    UNIT: bytes, total original payload
    quantum_bytes         : int    UNIT: bytes sent over quantum channel
    classical_bytes       : int    UNIT: bytes sent over classical channel

Transfer timing:
    transfer_elapsed_s    : float  UNIT: seconds; start of alice_transfer()
                                   until echo confirmed (includes reroute)
    throughput_bytes_per_s: float  UNIT: bytes/s, derived
    latency_s             : float  UNIT: seconds; same value as
                                   transfer_elapsed_s, kept under its own name
                                   because "latency" is the dashboard-facing
                                   end-to-end concept

Echo outcome:
    echo_outcome          : str    "CLEAN_PASS" |
                                   "RECOVERED_VIA_REROUTE" | "CHANNEL_FAILURE"
                                   or, on records written by record_failure(),
                                   an orchestrator-level terminal outcome:
                                   "SESSION_ABORTED" |
                                   "RECONCILIATION_INCOMPLETE" |
                                   "KEY_MISMATCH_ERROR" | "ERROR" | "TIMEOUT"
    echo_recovered        : bool   True iff success came via reroute
    mismatch_source       : str | null     "quantum" | "classical" | "both"
    echo_diagnosis        : str | null     "QUANTUM_CHANNEL_ISSUE" |
                                           "POSSIBLE_EAVESDROPPER"
    retransmit_bytes      : int    UNIT: bytes patched during reroute

Reconciliation (null when not observable; added in schema 1.1):
    bits_corrected        : int | null   UNIT: bits flipped by Cascade (Bob side;
                                         Alice always corrects 0)
    bits_sacrificed       : int | null   UNIT: bits trimmed for privacy
                                         amplification (identical on both sides)

Fault injection (all null/neutral when injector absent or disabled;
every modeled value carries the _simulated suffix):
    fault_injection_enabled    : bool
    ber_simulated              : float | null   dimensionless ratio
    snr_db_simulated           : float | null   UNIT: dB, BSC proxy,
                                                not physically measured
    plr_simulated              : float | null   dimensionless ratio
    bits_injected_errors       : int    UNIT: bits flipped across session
    packets_simulated_dropped  : int    UNIT: packets counted as drops

-----------------
Derived Metrics
-----------------
    skr_bits_per_second   = qkd_key_bits / qkd_elapsed_s
    throughput_bytes_per_s = payload_bytes / transfer_elapsed_s

Both guard the zero denominator with 0.0 (degenerate-but-valid record beats a
crashed write). Throughput is end-to-end application-layer throughput: it
INCLUDES Phase 6 reroute overhead, so it is not a channel-capacity figure.

-----------
Integration
-----------
  - scripts/run_benchmark.py: creates one collector per run on a timestamped
    .jsonl path; calls record_transfer() for completed transfers and
    record_failure() on every terminal failure path (abort, reconciliation,
    key mismatch, error, timeout); both calls are wrapped in try/except so a
    metrics problem never aborts the benchmark.
  - scripts/generate_dashboard.py: consumes the .jsonl lines and groups by
    echo_outcome, so failure records flow through as their own bucket.
  - tests/test_metrics.py: schema conformance, multi-record append behaviour,
    fault-field propagation.
  - tests/test_end_to_end.py: full-pipeline record assertions.
"""

from __future__ import annotations

import json
import math
import pathlib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# Make the project root importable when run outside package context.
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

# Public API of this module.
__all__ = ["TransferRecord", "MetricsCollector"]

# Default sink: alongside this module under metrics/logs/. A module-relative
# path keeps runs self-contained regardless of the caller's working directory.
_DEFAULT_LOG_PATH = pathlib.Path(__file__).parent / "logs" / "transfers.jsonl"

# Schema tag written into every record. Consumers (dashboard, tests) key off
# it; bump whenever a field is added, removed, or redefined.
SCHEMA_VERSION = "1.1"


@dataclass
class TransferRecord:
    r"""
    One record per completed transfer; serialized to a single JSON line.

    Every field has a default so a partially-instrumented caller can still
    produce a schema-valid record. Defaults encode neutral values: zero
    counters, null unknowns, and the split fractions of the all-classical
    fallback decision (quantum_fraction=0.0, classical_fraction=1.0).

    ----------
    Attributes
    ----------
    [Identity]
    schema_version : str
        SCHEMA_VERSION at construction time.
    session_id : str
        Fresh UUID4 per instance unless the caller supplies one; ties all
        transfers of one QKD session together in the log.
    transfer_id : str
        Fresh UUID4 per instance; unique row identity even for repeated
        identical transfers.
    timestamp_utc : str
        Wall-clock creation moment, UTC, microsecond resolution. Recorded at
        object construction (record-building time), not transfer start.

    [QKD]
    protocol : str
        Which QKD protocol produced the key ("BB84" or "E91").
    qber : float
        UNIT: dimensionless. Measured QBER from the QKD phase.
    chsh : float | None
        CHSH S parameter; None for BB84 runs (no Bell test performed).
    qkd_key_bits : int
        UNIT: bits. Length of the final reconciled key.
    qkd_elapsed_s : float
        UNIT: seconds. Caller-measured duration of node.run_qkd().
    skr_bits_per_second : float
        UNIT: bits/s. Derived by the collector, not caller-supplied.

    [Split]
    split_reason : str
        REASON_* token explaining the payload split decision.
    quantum_fraction, classical_fraction : float
        UNIT: dimensionless shares of the payload per channel; sum to 1.

    [Payload]
    payload_bytes : int
        UNIT: bytes. Original payload size before splitting.
    quantum_bytes, classical_bytes : int
        UNIT: bytes. Per-channel segment sizes (split.quantum_len etc.).

    [Timing]
    transfer_elapsed_s : float
        UNIT: seconds. Full transmit-plus-echo-validate wall clock.
    throughput_bytes_per_s : float
        UNIT: bytes/s. payload_bytes / transfer_elapsed_s.
    latency_s : float
        UNIT: seconds. Duplicate of transfer_elapsed_s under the name the
        dashboard plots; deliberate redundancy, not accidental.

    [Echo]
    echo_outcome : str
        Terminal status string; see module docstring for the vocabulary.
    echo_recovered : bool
        True when success required the Phase 6 patch/reroute path.
    mismatch_source : str | None
        Where corruption was found; None on clean passes.
    echo_diagnosis : str | None
        Echo-phase diagnosis token; None when nothing mismatched.
    retransmit_bytes : int
        UNIT: bytes retransmitted during recovery; 0 on clean passes.

    [Reconciliation]
    bits_corrected : int | None
        UNIT: bits. Bit flips applied by Cascade on Bob's side (Alice is the
        reference and always corrects 0). None when unobservable: legacy
        records, or failures before reconciliation ran.
    bits_sacrificed : int | None
        UNIT: bits. Key material discarded as privacy amplification for the
        parity bits revealed during parity exchanges. Identical on both
        sides by protocol definition. None under the same conditions.

    [Fault injection]
    fault_injection_enabled : bool
        True iff an enabled FaultInjector was passed to record_transfer().
    ber_simulated, plr_simulated : float | None
        UNIT: dimensionless ratios snapshotted from the injector.
    snr_db_simulated : float | None
        UNIT: dB. BSC-derived proxy; None also encodes "perfect channel".
    bits_injected_errors : int
        UNIT: bits. Session-cumulative flip count at snapshot time.
    packets_simulated_dropped : int
        UNIT: packets. Session-cumulative drop count at snapshot time.
    """

    # Identity
    schema_version: str = SCHEMA_VERSION
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    transfer_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    )

    # QKD
    protocol: str = "BB84"
    qber: float = 0.0
    chsh: float | None = None
    qkd_key_bits: int = 0
    qkd_elapsed_s: float = 0.0
    skr_bits_per_second: float = 0.0

    # Split
    split_reason: str = ""
    quantum_fraction: float = 0.0
    classical_fraction: float = 1.0

    # Payload
    payload_bytes: int = 0
    quantum_bytes: int = 0
    classical_bytes: int = 0

    # Timing
    transfer_elapsed_s: float = 0.0
    throughput_bytes_per_s: float = 0.0
    latency_s: float = 0.0

    # Echo
    echo_outcome: str = "CLEAN_PASS"
    echo_recovered: bool = False
    mismatch_source: str | None = None
    echo_diagnosis: str | None = None
    retransmit_bytes: int = 0

    # Reconciliation (null when caller could not observe it, e.g. legacy
    # records or failures before the reconciliation phase ran)
    bits_corrected: int | None = None
    bits_sacrificed: int | None = None

    # Fault injection (null when off)
    fault_injection_enabled: bool = False
    ber_simulated: float | None = None
    snr_db_simulated: float | None = None
    plr_simulated: float | None = None
    bits_injected_errors: int = 0
    packets_simulated_dropped: int = 0


class MetricsCollector:
    r"""
    Appends one JSON record per transfer to a .jsonl log file.

    Stateless apart from log_path: every fact flows in through
    record_transfer(), so collectors are cheap and multiple instances may
    point at the same file safely AS LONG AS writes stay single-threaded
    (append mode is not inter-process locked; the orchestration layer is
    single-threaded by design).

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
        """
        Point the collector at its sink.

        ----------
        Parameters
        ----------
        log_path : str | pathlib.Path | None
            Target .jsonl file. None selects _DEFAULT_LOG_PATH. The benchmark
            always passes a per-run timestamped path so scenario batches land
            in isolated files.
        """
        self.log_path = pathlib.Path(log_path) if log_path else _DEFAULT_LOG_PATH

    def record_transfer(
        self,
        *,
        session_id: str,
        protocol: str,
        qber: float,
        chsh: float | None,
        qkd_key_bits: int,
        qkd_elapsed_s: float,
        decision,  # SplitDecision
        payload_bytes: int,
        quantum_bytes: int,
        classical_bytes: int,
        transfer_elapsed_s: float,
        echo_result,  # EchoResult
        fault_injector=None,  # FaultInjector | None
        bits_corrected: int | None = None,
        bits_sacrificed: int | None = None,
        transfer_id: str | None = None,
    ) -> TransferRecord:
        r"""
        Build, persist, and return one TransferRecord.

        All parameters are keyword-only: with 16 parameters of mostly-numeric
        type, positional calls would be unreviewable and silently wrong under
        reordering. Keyword enforcement makes call sites self-documenting.

        ----------
        Parameters
        ----------
        [Direct measurements] (units per TransferRecord docs)
        qkd_elapsed_s, transfer_elapsed_s : float
            Caller-measured durations; rounded to 6 dp here (microsecond
            resolution, already beyond monotonic clock usefulness).
        decision : SplitDecision
            From split_controller; contributes reason and both fractions.
        echo_result : EchoResult
            From transmission.echo_validation; contributes outcome fields.
        fault_injector : FaultInjector | None
            When present AND enabled, session-cumulative counters are
            snapshotted into the record; otherwise the fault block stays
            null/False so disabled runs are indistinguishable from absent ones.
        bits_corrected, bits_sacrificed : int | None
            Reconciliation stats from Bob's ReconcileResult. Pass them when
            the reconciliation phase ran; leave as None otherwise so the
            record honestly reports "not observed" instead of a fake zero.

        transfer_id : str | None
            Caller-supplied id; a fresh UUID4 when omitted.

        -------
        Returns
        -------
        TransferRecord
            The exact record persisted, returned so callers/tests can assert
            on it without re-reading the log file.

        ---------------------
        Normalization Policy
        ---------------------
        Rates (skr, throughput) round to 2 dp, ratios to 6-8 dp: stable,
        human-scannable JSON rather than float noise, while keeping enough
        precision for dashboard aggregation.
        """
        # Zero-denominator guards: a degenerate timing input yields a valid
        # 0.0 rate instead of crashing the write path mid-benchmark.
        skr = qkd_key_bits / qkd_elapsed_s if qkd_elapsed_s > 0 else 0.0
        throughput = (
            payload_bytes / transfer_elapsed_s if transfer_elapsed_s > 0 else 0.0
        )

        # Consistency guards: a caller bug here would not crash anything, it
        # would silently poison every dashboard aggregate built from this row.
        # Split fractions must form a complete partition (float tolerance);
        # segment sizes must tile the payload exactly. Deliberately NOT
        # applied in record_failure(): failure rows legitimately carry
        # partial/neutral split data.
        if not math.isclose(
            decision.quantum_fraction + decision.classical_fraction, 1.0, abs_tol=1e-9
        ):
            raise ValueError(
                f"split fractions do not sum to 1: "
                f"{decision.quantum_fraction} + {decision.classical_fraction}"
            )
        if quantum_bytes + classical_bytes != payload_bytes:
            raise ValueError(
                f"segment sizes {quantum_bytes}+{classical_bytes} "
                f"!= payload_bytes {payload_bytes}"
            )

        # Map the boolean pair (success, recovered) onto the three-state
        # outcome vocabulary consumed by the dashboard.
        if echo_result.success and not echo_result.recovered:
            outcome = "CLEAN_PASS"
        elif echo_result.success and echo_result.recovered:
            outcome = "RECOVERED_VIA_REROUTE"
        else:
            outcome = "CHANNEL_FAILURE"

        # Snapshot the injector ONCE so every fault field in the record comes
        # from the same instant (ber/plr/error_bits would otherwise be read at
        # slightly different times if evaluated inline below).
        snap = self._fault_snapshot(fault_injector)

        rec = TransferRecord(
            session_id=session_id,
            transfer_id=transfer_id or str(uuid.uuid4()),
            protocol=protocol,
            qber=qber,
            chsh=chsh,
            qkd_key_bits=qkd_key_bits,
            qkd_elapsed_s=round(qkd_elapsed_s, 6),
            skr_bits_per_second=round(skr, 2),
            split_reason=decision.reason,
            quantum_fraction=decision.quantum_fraction,
            classical_fraction=decision.classical_fraction,
            payload_bytes=payload_bytes,
            quantum_bytes=quantum_bytes,
            classical_bytes=classical_bytes,
            transfer_elapsed_s=round(transfer_elapsed_s, 6),
            throughput_bytes_per_s=round(throughput, 2),
            latency_s=round(transfer_elapsed_s, 6),
            echo_outcome=outcome,
            echo_recovered=echo_result.recovered,
            mismatch_source=echo_result.mismatch_source,
            # EchoResult.diagnosis was renamed to echo_diagnosis at the source
            # (transmission/echo_validation.py); accessed directly, no shim.
            echo_diagnosis=echo_result.echo_diagnosis,
            retransmit_bytes=echo_result.retransmit_bytes,
            bits_corrected=bits_corrected,
            bits_sacrificed=bits_sacrificed,
            fault_injection_enabled=snap["enabled"],
            ber_simulated=round(snap["ber"], 8) if snap["ber"] is not None else None,
            snr_db_simulated=snap["snr_db"],
            plr_simulated=round(snap["plr"], 6) if snap["plr"] is not None else None,
            bits_injected_errors=snap["error_bits"],
            packets_simulated_dropped=snap["dropped_packets"],
        )

        self._write(rec)
        return rec

    def record_failure(
        self,
        *,
        session_id: str,
        outcome: str,
        protocol: str = "BB84",
        qber: float = 0.0,
        chsh: float | None = None,
        qkd_key_bits: int = 0,
        qkd_elapsed_s: float = 0.0,
        decision=None,  # SplitDecision | None
        payload_bytes: int = 0,
        quantum_bytes: int = 0,
        classical_bytes: int = 0,
        transfer_elapsed_s: float = 0.0,
        fault_injector=None,  # FaultInjector | None
    ) -> TransferRecord:
        r"""
        Build and persist a record for a transfer that FAILED before producing
        an EchoResult (session abort, reconciliation incomplete, key mismatch,
        timeout, or any orchestrator-level error).

        ----------
        Why It Exists
        ----------
        record_transfer() requires a completed echo phase; failures raised on
        the way there previously vanished from the log entirely, giving the
        .jsonl survivorship bias (success rate read as 100% by construction).
        This method records those outcomes so failure-rate curves are plottable.

        ----------
        Parameters
        ----------
        outcome : str
            Terminal outcome token; one of the orchestrator vocabulary
            "SESSION_ABORTED" | "RECONCILIATION_INCOMPLETE" | "KEY_MISMATCH_ERROR"
            | "ERROR" | "TIMEOUT". Stored in echo_outcome so dashboards keep
            one grouping column.
        Everything else is OPTIONAL and defaults to neutral: pass whatever the
        pipeline managed to compute before dying (qber/key bits once QKD ran,
        split sizes once splitting ran). Unknown fields stay at their safe
        defaults rather than fabricating zeros that look like measurements.

        -------
        Returns
        -------
        TransferRecord
            The persisted record, for caller assertions/tests.
        """
        # Zero-denominator guards, same policy as record_transfer().
        skr = qkd_key_bits / qkd_elapsed_s if qkd_elapsed_s > 0 else 0.0
        throughput = (
            payload_bytes / transfer_elapsed_s if transfer_elapsed_s > 0 else 0.0
        )
        snap = self._fault_snapshot(fault_injector)

        rec = TransferRecord(
            session_id=session_id,
            protocol=protocol,
            qber=qber,
            chsh=chsh,
            qkd_key_bits=qkd_key_bits,
            qkd_elapsed_s=round(qkd_elapsed_s, 6),
            skr_bits_per_second=round(skr, 2),
            split_reason=decision.reason if decision is not None else "",
            quantum_fraction=(
                decision.quantum_fraction if decision is not None else 0.0
            ),
            classical_fraction=(
                decision.classical_fraction if decision is not None else 1.0
            ),
            payload_bytes=payload_bytes,
            quantum_bytes=quantum_bytes,
            classical_bytes=classical_bytes,
            transfer_elapsed_s=round(transfer_elapsed_s, 6),
            throughput_bytes_per_s=round(throughput, 2),
            latency_s=round(transfer_elapsed_s, 6),
            echo_outcome=outcome,
            echo_recovered=False,
            mismatch_source=None,
            echo_diagnosis=None,
            retransmit_bytes=0,
            fault_injection_enabled=snap["enabled"],
            ber_simulated=round(snap["ber"], 8) if snap["ber"] is not None else None,
            snr_db_simulated=snap["snr_db"],
            plr_simulated=round(snap["plr"], 6) if snap["plr"] is not None else None,
            bits_injected_errors=snap["error_bits"],
            packets_simulated_dropped=snap["dropped_packets"],
        )

        self._write(rec)
        return rec

    def _fault_snapshot(self, fault_injector) -> dict:
        """
        Read every fault metric from the injector in ONE instant.

        ----------
        Parameters
        ----------
        fault_injector : FaultInjector | None

        -------
        Returns
        -------
        dict with keys enabled, ber, snr_db, plr, error_bits, dropped_packets.
        When the injector is absent or disabled, values are the neutral block
        (None rates, zero counters) so disabled runs are indistinguishable
        from absent ones downstream.
        """
        fi_enabled = fault_injector is not None and fault_injector.enabled
        return {
            "enabled": fi_enabled,
            "ber": fault_injector.ber if fi_enabled else None,
            "snr_db": fault_injector.snr_db if fi_enabled else None,
            "plr": fault_injector.plr if fi_enabled else None,
            "error_bits": fault_injector.error_bits if fi_enabled else 0,
            "dropped_packets": fault_injector.dropped_packets if fi_enabled else 0,
        }

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _write(self, rec: TransferRecord) -> None:
        r"""
        Append one record as a single terminated JSON line.

        -----
        Notes
        -----
        - mkdir(parents=True): first write creates metrics/logs/ lazily so a
          fresh checkout needs no setup step.
        - Append mode + trailing newline: the .jsonl contract (one complete
          JSON object per line) lets consumers stream the file with plain
          line iteration, including files from previous runs.
        - asdict() deep-converts the dataclass; the comprehension then
          replaces non-finite floats (snr_db_simulated can be -inf at
          BER >= 1) with None BEFORE json.dumps runs. This must happen here:
          the json encoder emits bare `Infinity` for such floats and never
          consults any default hook for float values, so a post-hoc serializer
          would never fire and invalid JSON tokens would reach the log.
        - TransferRecord is flat, so this one-level pass covers every field;
          if the schema ever nests containers, promote it to a recursive walk.
        """
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            key: (
                None if isinstance(value, float) and not math.isfinite(value) else value
            )
            for key, value in asdict(rec).items()
        }
        line = json.dumps(data) + "\n"
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
