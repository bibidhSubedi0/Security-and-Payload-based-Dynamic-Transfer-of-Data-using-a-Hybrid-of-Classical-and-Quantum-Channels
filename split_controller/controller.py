"""
Dynamic Split Controller (Phase 4)
==================================
Implements Fig. 4 of the project proposal (§6.2.3).

Given the current channel state and security preference, this module decides what
fraction of the payload to route over the quantum channel (superdense coding) versus
the classical channel (AES-256-GCM over TCP).  It produces a *decision* only; it
does not transmit anything (that is Phase 5).

-------------
Decision Tree
-------------
1. Sufficient ebits?       available_ebits < MIN_VIABLE_EBITS → 100 % classical
2. QBER acceptable?        qber > QBER_THRESHOLD               → 100 % classical
3. Security level branch (reached only when ebits ok AND QBER ok):
   "low"    → QUANTUM_FRACTION_LOW    (small quantum share)
   "medium" → QUANTUM_FRACTION_MEDIUM (moderate quantum share)
   "high"   → check payload vs ebit capacity:
               sufficient → MAX_QUANTUM_FRACTION
               constrained → min(ebit_capacity / payload_bits, MAX_QUANTUM_FRACTION)

------------------
Constant rationale
------------------
MIN_VIABLE_EBITS = 4
    4 ebits * 2 bits/ebit = 8 bits = 1 byte.  Anything smaller cannot form a
    useful quantum payload unit; the quantum-channel setup overhead would dominate.

BITS_PER_EBIT = 2
    Superdense coding theoretical capacity: 2 classical bits per qubit.  Established
    and verified in quantum/superdense_coding.py (Phase 1).

QUANTUM_FRACTION_LOW = 0.25
    "Low" security prioritises throughput over confidentiality.  25 % quantum gives
    a minimal QKD-secured segment (enough to demonstrate the hybrid path) while 75 %
    stays on the fast AES-256 classical channel.

QUANTUM_FRACTION_MEDIUM = 0.50
    Balanced trade-off: exactly half the payload gets full quantum treatment.
    This is the config default and the clearest expression of "moderate".

MAX_QUANTUM_FRACTION = 0.75
    Ceiling for "high" security, deliberately NOT 1.0.  The classical channel must
    always carry at least 25 % of the payload because:
      (a) Phase 6 echo / retransmission traffic needs a classical path.
      (b) QKD classical sifting messages travel over this channel.
      (c) Keeping 100 % quantum would make this a quantum-only system, defeating the
          "hybrid" premise of the project.
    0.75 is quantum-dominant while preserving the classical fallback.

-----------------------------------------------------
Ebit-capacity formula ("sufficient for full payload")
-----------------------------------------------------
    payload_bits      = payload_size_bytes * 8
    ebits_needed      = payload_bits / BITS_PER_EBIT   (= payload_size_bytes * 4)
    ebit_capacity_bits = available_ebits * BITS_PER_EBIT

If ebit_capacity_bits ≥ payload_bits → return MAX_QUANTUM_FRACTION.
Otherwise → quantum_fraction = ebit_capacity_bits / payload_bits,
            capped at MAX_QUANTUM_FRACTION.

All constants are module-level named values and can be overridden via the
`thresholds` dict accepted by `compute_split()` for Phase 8 benchmarking sweeps.
"""

from __future__ import annotations

import math
import sys
import pathlib
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from quantum.bb84 import BB84_QBER_THRESHOLD   # 0.11, single authoritative source

# ---------------------------------------------------------------------------
# Named constants (all sweepable via thresholds= parameter)
# ---------------------------------------------------------------------------

MIN_VIABLE_EBITS: int     = 4     # minimum ebit count for a useful quantum payload
BITS_PER_EBIT:    int     = 2     # superdense coding: 2 classical bits per qubit

QUANTUM_FRACTION_LOW:    float = 0.25   # "low"    security quantum share
QUANTUM_FRACTION_MEDIUM: float = 0.50   # "medium" security quantum share
MAX_QUANTUM_FRACTION:    float = 0.75   # "high"   security ceiling (see rationale above)

# Accepted values of compute_split()'s security_level parameter;
# anything else raises ValueError at the trust boundary.
VALID_SECURITY_LEVELS = frozenset({"low", "medium", "high"})


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SplitDecision:
    """
    Output of compute_split().

    ----------
    Attributes
    ----------
    quantum_fraction : float
        Share of payload routed via superdense coding over the quantum
        channel, in [0, 1].
    classical_fraction : float
        Complement share sent AES-256-GCM encrypted over the classical
        channel; quantum_fraction + classical_fraction == 1.0 always
        (enforced in __post_init__).
    reason : str
        One of the REASON_* tokens below: short uppercase strings suitable
        for structured logging (Phase 7) and metrics grouping
        (generate_dashboard.py maps them back to coarse levels).

    -----
    Notes
    -----
    Frozen so a decision cannot be mutated after the fact; tests and the
    benchmark rely on decisions being immutable records.
    """
    quantum_fraction:  float
    classical_fraction: float
    reason: str   # one of the REASON_* strings below

    def __post_init__(self) -> None:
        if abs(self.quantum_fraction + self.classical_fraction - 1.0) > 1e-9:
            raise ValueError(
                f"Fractions do not sum to 1.0: "
                f"{self.quantum_fraction} + {self.classical_fraction}"
            )
        if not (0.0 <= self.quantum_fraction <= 1.0):
            raise ValueError(f"quantum_fraction out of [0,1]: {self.quantum_fraction}")


# Reason tokens: the complete outcome vocabulary of a split decision.
# EBIT_INSUFFICIENT / QBER_EXCEEDED  -> forced 100% classical (degraded mode)
# LOW/MEDIUM/HIGH_SECURITY_*         -> policy-driven split
# HIGH_SECURITY_CONSTRAINED          -> high security, capped by ebit budget
# Use these constants in tests/consumers to avoid typo drift.
REASON_EBIT_INSUFFICIENT      = "EBIT_INSUFFICIENT"
REASON_QBER_EXCEEDED          = "QBER_EXCEEDED"
REASON_LOW_SECURITY           = "LOW_SECURITY"
REASON_MEDIUM_SECURITY        = "MEDIUM_SECURITY"
REASON_HIGH_SECURITY_MAX      = "HIGH_SECURITY_MAX"
REASON_HIGH_SECURITY_CONSTRAINED = "HIGH_SECURITY_CONSTRAINED"


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def compute_split(
    security_level: str,
    payload_size_bytes: int,
    available_ebits: int,
    qber: float,
    thresholds: dict | None = None,
) -> SplitDecision:
    """
    Compute the quantum/classical payload split ratio.

    ----------
    Parameters
    ----------
    security_level    : "low" | "medium" | "high"
    payload_size_bytes: total payload size in bytes
    available_ebits   : number of ebits currently held by this session
    qber              : measured QBER from the QKD phase (0.0-1.0)
    thresholds        : optional dict of override values for any named constant;
                        keys match the module-level constant names (lowercase):
                        "min_viable_ebits", "qber_threshold",
                        "quantum_fraction_low", "quantum_fraction_medium",
                        "max_quantum_fraction", "bits_per_ebit"

    -------
    Returns
    -------
    SplitDecision with quantum_fraction, classical_fraction, and a reason token.

    ------
    Raises
    ------
    ValueError if security_level is not one of "low", "medium", "high", or if
    numeric inputs are out of range: qber must be finite in [0, 1] (a NaN
    comparison is always False, so an unvalidated NaN would silently pass the
    security check), payload_size_bytes >= 1, available_ebits >= 0.
    """
    if security_level not in VALID_SECURITY_LEVELS:
        raise ValueError(
            f"Unknown security_level {security_level!r}. "
            f"Must be one of {sorted(VALID_SECURITY_LEVELS)}."
        )
    # Numeric validation at the trust boundary: callers feed user-supplied
    # demo/benchmark values straight into this function.
    if not (isinstance(qber, (int, float)) and math.isfinite(qber)
            and 0.0 <= qber <= 1.0):
        raise ValueError(f"qber must be a finite value in [0, 1], got {qber!r}")
    if payload_size_bytes < 1:
        raise ValueError(f"payload_size_bytes must be >= 1, got {payload_size_bytes}")
    if available_ebits < 0:
        raise ValueError(f"available_ebits must be >= 0, got {available_ebits}")

    t = _resolve_thresholds(thresholds)

    # ------------------------------------------------------------------
    # Step 1: Sufficient ebits?
    # ------------------------------------------------------------------
    if available_ebits < t["min_viable_ebits"]:
        return _classical(REASON_EBIT_INSUFFICIENT)

    # ------------------------------------------------------------------
    # Step 2: QBER within acceptable threshold?
    # (strictly greater-than: QBER == threshold is still considered secure,
    #  consistent with ebit_server.py's  `secure = qber <= BB84_QBER_THRESHOLD`)
    # ------------------------------------------------------------------
    if qber > t["qber_threshold"]:
        return _classical(REASON_QBER_EXCEEDED)

    # ------------------------------------------------------------------
    # Step 3: Security level branch
    # ------------------------------------------------------------------
    if security_level == "low":
        q = t["quantum_fraction_low"]
        return _split(q, REASON_LOW_SECURITY)

    if security_level == "medium":
        q = t["quantum_fraction_medium"]
        return _split(q, REASON_MEDIUM_SECURITY)

    # security_level == "high"
    payload_bits       = payload_size_bytes * 8
    ebit_capacity_bits = available_ebits * t["bits_per_ebit"]

    if ebit_capacity_bits >= payload_bits:
        # Enough ebits to cover the entire payload → use maximum fraction
        return _split(t["max_quantum_fraction"], REASON_HIGH_SECURITY_MAX)

    # Constrained by available ebits
    q = ebit_capacity_bits / payload_bits          # raw fraction in (0, 1)
    q = min(q, t["max_quantum_fraction"])          # hard ceiling
    return _split(q, REASON_HIGH_SECURITY_CONSTRAINED)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _resolve_thresholds(overrides: dict | None) -> dict:
    """
    Merge module-level defaults with caller overrides.

    ----------
    Parameters
    ----------
    overrides : dict | None
        Keys match the lowercase names below (e.g. "min_viable_ebits").
        Unknown keys raise ValueError immediately: a typo'd key would
        otherwise fail silently and the sweep would benchmark the default
        policy while believing it overrode it. Exists so Phase 8 benchmark
        sweeps can probe alternate policies without editing this module.

    -------
    Returns
    -------
    dict
        Fresh merged mapping; the module constants themselves are never
        mutated.
    """
    defaults = {
        "min_viable_ebits":        MIN_VIABLE_EBITS,
        "qber_threshold":          BB84_QBER_THRESHOLD,
        "quantum_fraction_low":    QUANTUM_FRACTION_LOW,
        "quantum_fraction_medium": QUANTUM_FRACTION_MEDIUM,
        "max_quantum_fraction":    MAX_QUANTUM_FRACTION,
        "bits_per_ebit":           BITS_PER_EBIT,
    }
    if overrides:
        unknown = set(overrides) - set(defaults)
        if unknown:
            raise ValueError(
                f"Unknown threshold key(s) {sorted(unknown)}; "
                f"valid keys are {sorted(defaults)}."
            )
        defaults.update(overrides)
    return defaults


def _classical(reason: str) -> SplitDecision:
    """Degraded-mode decision: everything on the classical channel."""
    return SplitDecision(quantum_fraction=0.0, classical_fraction=1.0, reason=reason)


def _split(q: float, reason: str) -> SplitDecision:
    """
    Build a policy-driven decision for quantum share `q`.

    Classical fraction is computed as the complement (not stored
    independently) so rounding can never make the pair drift apart.
    """
    return SplitDecision(
        quantum_fraction=q,
        classical_fraction=1.0 - q,
        reason=reason,
    )
