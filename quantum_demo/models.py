from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class SplitInfo:
    quantum_fraction: float
    classical_fraction: float
    reason: str


@dataclass
class SessionResult:
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
