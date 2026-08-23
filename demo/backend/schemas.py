from pydantic import BaseModel, Field
from typing import Optional


class RunSessionRequest(BaseModel):
    protocol: str = Field(default="BB84", pattern="^(BB84|E91)$")
    security_level: str = Field(default="medium", pattern="^(low|medium|high)$")
    noise: float = Field(default=0.0, ge=0.0, le=1.0)
    payload_text: str = Field(default="hello-panel-demo", max_length=2048)


class RunSessionResponse(BaseModel):
    outcome: str
    protocol: str
    qber: Optional[float] = None
    chsh: Optional[float] = None
    skr: Optional[float] = None
    quantum_fraction: Optional[float] = None
    classical_fraction: Optional[float] = None
    split_reason: Optional[str] = None
    throughput_bps: Optional[float] = None
    latency_s: Optional[float] = None
    abort_reason: Optional[str] = None
    log_lines: list[str] = Field(default_factory=list)
