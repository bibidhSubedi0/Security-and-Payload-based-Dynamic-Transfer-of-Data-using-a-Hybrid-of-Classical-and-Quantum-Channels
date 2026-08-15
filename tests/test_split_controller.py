"""
Phase 4 — Dynamic Split Controller tests.

One test per branch of the decision tree, plus property-style and edge-case tests.
All tests use named constants from the module rather than magic numbers so the
assertions stay correct if constants are updated.
"""

import random

import pytest

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from split_controller.controller import (
    compute_split,
    SplitDecision,
    MIN_VIABLE_EBITS,
    BITS_PER_EBIT,
    QUANTUM_FRACTION_LOW,
    QUANTUM_FRACTION_MEDIUM,
    MAX_QUANTUM_FRACTION,
    REASON_EBIT_INSUFFICIENT,
    REASON_QBER_EXCEEDED,
    REASON_LOW_SECURITY,
    REASON_MEDIUM_SECURITY,
    REASON_HIGH_SECURITY_MAX,
    REASON_HIGH_SECURITY_CONSTRAINED,
)
from quantum.bb84 import BB84_QBER_THRESHOLD   # 0.11 — single source of truth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GOOD_QBER  = 0.0          # noiseless channel
GOOD_EBITS = 50           # arbitrary "plenty" of ebits
SMALL_PAYLOAD = 10        # bytes — easy to cover fully with modest ebits
LARGE_PAYLOAD = 1024      # bytes — hard to cover with 50 ebits


def _ebits_for_full_payload(payload_bytes: int) -> int:
    """Minimum ebits to cover the entire payload via superdense coding."""
    payload_bits = payload_bytes * 8
    return (payload_bits + BITS_PER_EBIT - 1) // BITS_PER_EBIT   # ceiling division


# ---------------------------------------------------------------------------
# Branch 1: Insufficient ebits → 100 % classical
# ---------------------------------------------------------------------------

def test_zero_ebits_routes_classical():
    d = compute_split("high", LARGE_PAYLOAD, 0, GOOD_QBER)
    print(f"\n[split] zero ebits → {d}")
    assert d.quantum_fraction == 0.0
    assert d.classical_fraction == 1.0
    assert d.reason == REASON_EBIT_INSUFFICIENT


def test_ebits_below_floor_routes_classical():
    """available_ebits = MIN_VIABLE_EBITS - 1 must still be rejected."""
    d = compute_split("high", LARGE_PAYLOAD, MIN_VIABLE_EBITS - 1, GOOD_QBER)
    print(f"\n[split] ebits={MIN_VIABLE_EBITS - 1} (floor={MIN_VIABLE_EBITS}) → {d}")
    assert d.reason == REASON_EBIT_INSUFFICIENT
    assert d.quantum_fraction == 0.0


def test_ebits_at_floor_does_not_route_classical():
    """Exactly MIN_VIABLE_EBITS should NOT be rejected by the ebit check."""
    d = compute_split("low", LARGE_PAYLOAD, MIN_VIABLE_EBITS, GOOD_QBER)
    assert d.reason != REASON_EBIT_INSUFFICIENT
    assert d.quantum_fraction > 0.0


def test_insufficient_ebits_overrides_security_level():
    """Ebit check fires before security level — even 'high' gets 100% classical."""
    for level in ("low", "medium", "high"):
        d = compute_split(level, LARGE_PAYLOAD, 0, GOOD_QBER)
        assert d.reason == REASON_EBIT_INSUFFICIENT, (
            f"level={level!r} with 0 ebits should be EBIT_INSUFFICIENT"
        )


# ---------------------------------------------------------------------------
# Branch 2: QBER over threshold → 100 % classical
# ---------------------------------------------------------------------------

def test_qber_over_threshold_routes_classical():
    noisy_qber = BB84_QBER_THRESHOLD + 0.01   # 0.12
    d = compute_split("high", LARGE_PAYLOAD, GOOD_EBITS, noisy_qber)
    print(f"\n[split] qber={noisy_qber} (threshold={BB84_QBER_THRESHOLD}) → {d}")
    assert d.quantum_fraction == 0.0
    assert d.classical_fraction == 1.0
    assert d.reason == REASON_QBER_EXCEEDED


def test_qber_exactly_at_threshold_is_acceptable():
    """QBER == 0.11 is still within threshold (> not >=), consistent with ebit_server."""
    d = compute_split("medium", LARGE_PAYLOAD, GOOD_EBITS, BB84_QBER_THRESHOLD)
    print(f"\n[split] qber=threshold={BB84_QBER_THRESHOLD} → {d}")
    assert d.reason != REASON_QBER_EXCEEDED
    assert d.quantum_fraction > 0.0


def test_qber_exceeded_overrides_high_security_and_ebits():
    """High noise routes 100% classical even with plenty of ebits and high security."""
    very_noisy = 0.50
    d = compute_split("high", SMALL_PAYLOAD, 10_000, very_noisy)
    assert d.reason == REASON_QBER_EXCEEDED
    assert d.quantum_fraction == 0.0


# ---------------------------------------------------------------------------
# Branch 3a: Low security
# ---------------------------------------------------------------------------

def test_low_security_good_conditions():
    d = compute_split("low", LARGE_PAYLOAD, GOOD_EBITS, GOOD_QBER)
    print(f"\n[split] low security → {d}")
    assert d.quantum_fraction == QUANTUM_FRACTION_LOW
    assert d.classical_fraction == 1.0 - QUANTUM_FRACTION_LOW
    assert d.reason == REASON_LOW_SECURITY


def test_low_security_fraction_in_expected_range():
    """'Small' quantum fraction: between 0 and 0.4 (clearly below medium)."""
    d = compute_split("low", LARGE_PAYLOAD, GOOD_EBITS, GOOD_QBER)
    assert 0.0 < d.quantum_fraction < QUANTUM_FRACTION_MEDIUM, (
        f"Low-security quantum fraction {d.quantum_fraction} not in (0, {QUANTUM_FRACTION_MEDIUM})"
    )


# ---------------------------------------------------------------------------
# Branch 3b: Medium security
# ---------------------------------------------------------------------------

def test_medium_security_good_conditions():
    d = compute_split("medium", LARGE_PAYLOAD, GOOD_EBITS, GOOD_QBER)
    print(f"\n[split] medium security → {d}")
    assert d.quantum_fraction == QUANTUM_FRACTION_MEDIUM
    assert d.classical_fraction == 1.0 - QUANTUM_FRACTION_MEDIUM
    assert d.reason == REASON_MEDIUM_SECURITY


def test_medium_fraction_between_low_and_high():
    d = compute_split("medium", LARGE_PAYLOAD, GOOD_EBITS, GOOD_QBER)
    assert QUANTUM_FRACTION_LOW < d.quantum_fraction < MAX_QUANTUM_FRACTION, (
        f"Medium fraction {d.quantum_fraction} not strictly between "
        f"low ({QUANTUM_FRACTION_LOW}) and max ({MAX_QUANTUM_FRACTION})"
    )


# ---------------------------------------------------------------------------
# Branch 3c: High security — sufficient ebits
# ---------------------------------------------------------------------------

def test_high_security_sufficient_ebits_uses_max_fraction():
    """Enough ebits to cover the entire payload → return MAX_QUANTUM_FRACTION."""
    ebits_needed = _ebits_for_full_payload(SMALL_PAYLOAD)
    d = compute_split("high", SMALL_PAYLOAD, ebits_needed, GOOD_QBER)
    print(f"\n[split] high security, sufficient ebits ({ebits_needed} for "
          f"{SMALL_PAYLOAD}B) → {d}")
    assert d.quantum_fraction == MAX_QUANTUM_FRACTION
    assert d.reason == REASON_HIGH_SECURITY_MAX


def test_high_security_excess_ebits_still_uses_max_fraction():
    """Far more ebits than needed — still capped at MAX_QUANTUM_FRACTION."""
    d = compute_split("high", SMALL_PAYLOAD, 100_000, GOOD_QBER)
    assert d.quantum_fraction == MAX_QUANTUM_FRACTION
    assert d.reason == REASON_HIGH_SECURITY_MAX


# ---------------------------------------------------------------------------
# Branch 3d: High security — constrained by available ebits
# ---------------------------------------------------------------------------

def test_high_security_constrained_ebits_uses_ebit_fraction():
    """
    50 ebits × 2 bits/ebit = 100 bits.
    1024 bytes = 8192 bits.
    raw_fraction = 100 / 8192 ≈ 0.0122 → well below MAX_QUANTUM_FRACTION.
    """
    payload  = LARGE_PAYLOAD   # 1024 bytes
    ebits    = 50
    expected_q = (ebits * BITS_PER_EBIT) / (payload * 8)

    d = compute_split("high", payload, ebits, GOOD_QBER)
    print(f"\n[split] high security, constrained: ebits={ebits}, "
          f"payload={payload}B → {d}  (expected_q≈{expected_q:.6f})")

    assert d.reason == REASON_HIGH_SECURITY_CONSTRAINED
    assert abs(d.quantum_fraction - expected_q) < 1e-9, (
        f"Expected quantum_fraction≈{expected_q:.6f}, got {d.quantum_fraction:.6f}"
    )
    assert d.quantum_fraction < MAX_QUANTUM_FRACTION


def test_high_security_constrained_not_equal_to_max():
    """Constrained result must differ from MAX_QUANTUM_FRACTION."""
    d = compute_split("high", LARGE_PAYLOAD, GOOD_EBITS, GOOD_QBER)
    assert d.quantum_fraction != MAX_QUANTUM_FRACTION


def test_high_security_constrained_fraction_matches_formula():
    """Cross-check the exact formula: q = (ebits * BITS_PER_EBIT) / (payload_bytes * 8)."""
    for ebits, payload in [(10, 512), (30, 2048), (1, 100)]:
        raw_q = (ebits * BITS_PER_EBIT) / (payload * 8)
        expected_q = min(raw_q, MAX_QUANTUM_FRACTION)
        d = compute_split("high", payload, ebits, GOOD_QBER)
        if d.reason == REASON_HIGH_SECURITY_CONSTRAINED:
            assert abs(d.quantum_fraction - expected_q) < 1e-9, (
                f"ebits={ebits}, payload={payload}: "
                f"expected {expected_q:.6f}, got {d.quantum_fraction:.6f}"
            )


# ---------------------------------------------------------------------------
# Security level ordering
# ---------------------------------------------------------------------------

def test_security_level_ordering():
    """
    When ebits are NOT the bottleneck (plenty of ebits for a small payload),
    security level determines the fraction: low < medium < high (= MAX_QUANTUM_FRACTION).

    Note: with a large payload and few ebits, 'high' can legitimately produce a
    *smaller* fraction than 'low' because the ebit constraint overrides the level.
    That's correct behaviour — tested separately in test_high_security_constrained_*.
    This test isolates the ordering that holds when ebits are unconstrained.
    """
    # Use a payload small enough that GOOD_EBITS covers it fully
    # GOOD_EBITS=50, BITS_PER_EBIT=2 → capacity = 100 bits = 12.5 bytes → payload=10B
    unconstrained_payload = SMALL_PAYLOAD  # 10 bytes; 50 ebits >> needed (40 ebits)

    d_low  = compute_split("low",    unconstrained_payload, GOOD_EBITS, GOOD_QBER)
    d_med  = compute_split("medium", unconstrained_payload, GOOD_EBITS, GOOD_QBER)
    d_high = compute_split("high",   unconstrained_payload, GOOD_EBITS, GOOD_QBER)

    print(f"\n[split ordering] low={d_low.quantum_fraction:.4f}  "
          f"med={d_med.quantum_fraction:.4f}  "
          f"high={d_high.quantum_fraction:.4f}")

    assert d_low.quantum_fraction < d_med.quantum_fraction, (
        "low fraction must be < medium fraction (unconstrained ebits)"
    )
    assert d_med.quantum_fraction < d_high.quantum_fraction, (
        "medium fraction must be < high fraction (unconstrained ebits)"
    )
    assert d_high.quantum_fraction == MAX_QUANTUM_FRACTION


# ---------------------------------------------------------------------------
# Property-style: fractions always sum to 1.0
# ---------------------------------------------------------------------------

def test_fractions_always_sum_to_one():
    """
    Exhaustive random sweep: quantum_fraction + classical_fraction == 1.0
    for any combination of valid inputs, including edge cases.
    """
    rng = random.Random(0)
    levels = ["low", "medium", "high"]
    trials = 500
    for i in range(trials):
        level   = rng.choice(levels)
        payload = rng.randint(1, 65536)
        ebits   = rng.randint(0, 200)
        qber    = rng.uniform(0.0, 0.5)
        d = compute_split(level, payload, ebits, qber)
        total = d.quantum_fraction + d.classical_fraction
        assert abs(total - 1.0) < 1e-9, (
            f"Trial {i}: {level}, payload={payload}, ebits={ebits}, qber={qber:.4f} "
            f"→ fractions sum to {total} (not 1.0)"
        )
    print(f"\n[property] fractions summed to 1.0 across {trials} random inputs ✓")


def test_fractions_always_in_unit_interval():
    """Both fractions must always be in [0.0, 1.0]."""
    rng = random.Random(42)
    for _ in range(200):
        d = compute_split(
            rng.choice(["low", "medium", "high"]),
            rng.randint(1, 65536),
            rng.randint(0, 500),
            rng.uniform(0.0, 0.5),
        )
        assert 0.0 <= d.quantum_fraction <= 1.0
        assert 0.0 <= d.classical_fraction <= 1.0


# ---------------------------------------------------------------------------
# Threshold override / config-sweepability
# ---------------------------------------------------------------------------

def test_custom_qber_threshold_override():
    """A tighter custom QBER threshold should reject a QBER that the default allows."""
    qber = 0.05   # below default 0.11 (normally fine)
    d_default = compute_split("high", LARGE_PAYLOAD, GOOD_EBITS, qber)
    assert d_default.reason != REASON_QBER_EXCEEDED   # passes with default threshold

    # Override to a tighter threshold of 0.03
    d_tight = compute_split(
        "high", LARGE_PAYLOAD, GOOD_EBITS, qber,
        thresholds={"qber_threshold": 0.03},
    )
    print(f"\n[threshold override] qber={qber}, tight_threshold=0.03 → {d_tight}")
    assert d_tight.reason == REASON_QBER_EXCEEDED


def test_custom_max_fraction_override():
    """Overriding MAX_QUANTUM_FRACTION should be reflected in the output."""
    ebits_needed = _ebits_for_full_payload(SMALL_PAYLOAD)
    d = compute_split(
        "high", SMALL_PAYLOAD, ebits_needed, GOOD_QBER,
        thresholds={"max_quantum_fraction": 0.90},
    )
    print(f"\n[threshold override] max_fraction=0.90 → {d}")
    assert d.quantum_fraction == pytest.approx(0.90)


def test_custom_min_viable_ebits_override():
    """A higher floor should reject what the default floor would allow."""
    d_default = compute_split("medium", LARGE_PAYLOAD, MIN_VIABLE_EBITS, GOOD_QBER)
    assert d_default.reason != REASON_EBIT_INSUFFICIENT  # passes with default floor

    d_strict = compute_split(
        "medium", LARGE_PAYLOAD, MIN_VIABLE_EBITS, GOOD_QBER,
        thresholds={"min_viable_ebits": MIN_VIABLE_EBITS + 1},
    )
    assert d_strict.reason == REASON_EBIT_INSUFFICIENT


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_unknown_security_level_raises():
    with pytest.raises(ValueError, match="Unknown security_level"):
        compute_split("ultra", LARGE_PAYLOAD, GOOD_EBITS, GOOD_QBER)


def test_valid_security_levels_do_not_raise():
    for level in ("low", "medium", "high"):
        compute_split(level, LARGE_PAYLOAD, GOOD_EBITS, GOOD_QBER)  # must not raise
