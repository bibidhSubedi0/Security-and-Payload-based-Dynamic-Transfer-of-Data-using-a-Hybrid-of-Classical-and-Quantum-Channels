"""Tests for E91 entanglement-based QKD and CHSH inequality."""

import math
from quantum.e91 import run_e91


QUANTUM_BOUND = 2 * math.sqrt(2)   # ≈ 2.828
CLASSICAL_BOUND = 2.0


def test_chsh_exceeds_classical_bound():
    """S must exceed 2 for a genuine entangled channel."""
    result = run_e91(n_pairs_per_setting=800, seed=0)
    print(f"\n[E91] S={result.chsh_s:.4f}  "
          f"(classical bound={CLASSICAL_BOUND}, quantum bound≈{QUANTUM_BOUND:.3f})")
    print(f"      correlations: {result.correlations}")
    assert result.exceeds_classical, (
        f"|S|={abs(result.chsh_s):.4f} does not exceed classical bound of 2"
    )


def test_chsh_near_quantum_bound():
    """S should be close to 2√2 ≈ 2.828 for a perfect singlet state."""
    result = run_e91(n_pairs_per_setting=1000, seed=1)
    print(f"\n[E91] S={result.chsh_s:.4f}  quantum bound≈{QUANTUM_BOUND:.4f}")
    assert abs(result.chsh_s) >= 2.5, (
        f"Expected |S| ≥ 2.5 for Bell state, got {abs(result.chsh_s):.4f}"
    )


def test_chsh_within_absolute_quantum_bound():
    """S cannot exceed 2√2 — physics check."""
    result = run_e91(n_pairs_per_setting=500, seed=2)
    assert abs(result.chsh_s) <= QUANTUM_BOUND + 0.05, (
        f"|S|={abs(result.chsh_s):.4f} exceeds quantum bound {QUANTUM_BOUND:.4f}"
    )


def test_all_correlations_present():
    result = run_e91(n_pairs_per_setting=100, seed=3)
    assert len(result.correlations) == 4, (
        f"Expected 4 correlation values, got {len(result.correlations)}"
    )
    for key, val in result.correlations.items():
        assert -1.0 <= val <= 1.0, f"Correlation {key}={val} out of [-1,1]"
