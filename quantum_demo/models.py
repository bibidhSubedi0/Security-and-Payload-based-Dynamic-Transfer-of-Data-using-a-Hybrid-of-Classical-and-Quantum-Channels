"""
Result Models for the Quantum Demo
==================================

Lightweight, JSON-serializable dataclasses returned by
quantum_demo.pipeline.run_session(). They exist so the demo server and the
React frontend can consume one session's outcome without importing any
pipeline internals (Nodes, channels, injectors stay behind the boundary).

Field vocabulary mirrors scripts/run_benchmark.py so demo records and
benchmark records stay directly comparable.
"""

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class SplitInfo:
    """
    The dynamic split decision, flattened for transport.

    ----------
    Attributes
    ----------
    quantum_fraction : float
        Share of the payload sent via superdense coding over the quantum
        channel (0.0 to 1.0); mirrors compute_split()'s decision.
    classical_fraction : float
        Share sent AES-GCM encrypted over the classical channel;
        quantum_fraction + classical_fraction == 1.0.
    reason : str
        Why this split was chosen, e.g. "MEDIUM_SECURITY", "EBIT_CONSTRAINED",
        "QBER_UNSAFE". Consumed by metrics grouping (see
        generate_dashboard.py SPLIT_REASON_TO_LEVEL).
    """

    quantum_fraction: float
    classical_fraction: float
    reason: str


@dataclass
class SessionResult:
    """
    Complete outcome of one run_session() call.

    ----------
    Attributes
    ----------
    outcome : str
        One of:
          CLEAN_PASS            echo validated on first attempt
          RECOVERED_VIA_REROUTE echo validated after in-transfer recovery
          CHANNEL_FAILURE       echo digest mismatch (integrity lost)
          SESSION_ABORTED       QBER above 0.11, no key derived
          RECON_INCOMPLETE      nothing left after privacy amplification
          ERROR                 unexpected exception (see abort_reason)
          TIMEOUT               worker threads still alive after timeout_s
    protocol : str
        QKD protocol used: "BB84" | "E91".
    qber : Optional[float]
        Measured Quantum Bit Error Rate; None when QKD never completed.
    chsh : Optional[float]
        CHSH parameter (E91 only); always None for BB84 sessions.
    skr : Optional[float]
        Secret Key Rate in bits/s, approximated as key_bits / qkd_elapsed_s;
        None when timing or key is unavailable.
    split : Optional[SplitInfo]
        The split decision; None when the run aborted before compute_split().
    throughput_bps : Optional[float]
        payload_bytes / transfer_elapsed_s; None when no transfer happened.
    latency_s : Optional[float]
        End-to-end transfer wall time in seconds; None when no transfer.
    abort_reason : Optional[str]
        Human-readable cause for non-success outcomes; None on success.
    log_lines : List[str]
        Every structured log line captured during the session, in emission
        order; feeds the frontend's LogTerminal.
    bob_payload : Optional[bytes]
        What Node B actually received. Present for test/demo verification
        (compare against the payload Alice sent); normally None outside
        successful transfers.
    """

    outcome: str                # "CLEAN_PASS" | "SESSION_ABORTED" | "RECON_INCOMPLETE"
    protocol: str               # "BB84" | "E91"
    qber: Optional[float]
    chsh: Optional[float]
    skr: Optional[float]
    split: Optional[SplitInfo]
    throughput_bps: Optional[float]
    latency_s: Optional[float]
    abort_reason: Optional[str]
    log_lines: List[str] = field(default_factory=list)
    bob_payload: Optional[bytes] = None   # what Node B actually received (test/demo verification)
