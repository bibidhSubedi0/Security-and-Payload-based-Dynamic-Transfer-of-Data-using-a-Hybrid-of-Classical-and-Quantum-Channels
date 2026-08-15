"""Tests for BB84 quantum key distribution."""

import pytest
from quantum.bb84 import run_bb84, BB84_QBER_THRESHOLD, BB84_CHECK_FRACTION


def test_noiseless_qber_near_zero():
    result = run_bb84(n_qubits=400, injected_noise=0.0, seed=42)
    print(f"\n[BB84 noiseless] QBER={result.qber:.4f}, "
          f"sample_size={result.qber_sample_size}, "
          f"sifted_key_len={len(result.sifted_key)}")
    assert result.qber < 0.05, (
        f"Expected QBER < 5% with no noise, got {result.qber:.4f}"
    )


def test_noiseless_sifted_key_nonempty():
    result = run_bb84(n_qubits=200, injected_noise=0.0, seed=7)
    assert len(result.sifted_key) > 0, "Sifted key should not be empty"


def test_noiseless_basis_agreement():
    """All bits in sifted key came from matching bases — verify by re-checking."""
    result = run_bb84(n_qubits=200, injected_noise=0.0, seed=99)
    # With no noise, Alice's bit at each key position == Bob's result
    matching = [
        i for i in range(len(result.alice_bits))
        if result.alice_bases[i] == result.bob_bases[i]
    ]
    split = max(1, int(len(matching) * BB84_CHECK_FRACTION))
    key_positions = matching[split:]
    for pos in key_positions:
        assert result.alice_bits[pos] == result.bob_results[pos], (
            f"Mismatch at matching-basis position {pos} with no noise"
        )


def test_noisy_qber_rises():
    """QBER should rise proportionally with injected noise."""
    result_low  = run_bb84(n_qubits=600, injected_noise=0.05, seed=1)
    result_high = run_bb84(n_qubits=600, injected_noise=0.20, seed=1)
    print(f"\n[BB84 noisy] noise=5%  → QBER={result_low.qber:.4f}")
    print(f"[BB84 noisy] noise=20% → QBER={result_high.qber:.4f}")
    assert result_high.qber > result_low.qber, (
        "Higher injected noise should produce higher QBER"
    )


def test_qber_above_threshold_flagged():
    """QBER > 11% should be flagged as insecure."""
    result = run_bb84(n_qubits=600, injected_noise=0.30, seed=5)
    print(f"\n[BB84 threshold] noise=30% → QBER={result.qber:.4f}, "
          f"threshold={BB84_QBER_THRESHOLD}")
    insecure = result.qber > BB84_QBER_THRESHOLD
    assert insecure, (
        f"30% injected noise should push QBER above threshold "
        f"({BB84_QBER_THRESHOLD}), got {result.qber:.4f}"
    )


def test_qber_below_threshold_at_low_noise():
    """QBER < 11% with very low noise — channel is still considered secure."""
    result = run_bb84(n_qubits=600, injected_noise=0.05, seed=3)
    print(f"\n[BB84 threshold] noise=5% → QBER={result.qber:.4f}")
    assert result.qber < BB84_QBER_THRESHOLD, (
        f"5% injected noise should keep QBER below threshold, got {result.qber:.4f}"
    )


# ---------------------------------------------------------------------------
# BB84_CHECK_FRACTION tests (Phase 8)
# ---------------------------------------------------------------------------

def test_check_fraction_constant_exported():
    """BB84_CHECK_FRACTION must be exported, in (0, 1), and ≥ 0.50."""
    assert 0.0 < BB84_CHECK_FRACTION < 1.0, (
        f"BB84_CHECK_FRACTION={BB84_CHECK_FRACTION} must be in (0, 1)"
    )
    assert BB84_CHECK_FRACTION >= 0.50, (
        "Check fraction should use a majority of sifted bits for reliable QBER estimation"
    )
    print(f"\n[check_fraction] BB84_CHECK_FRACTION={BB84_CHECK_FRACTION}")


def test_check_sample_size_matches_fraction():
    """
    qber_sample_size returned by run_bb84() must equal
    max(1, int(len(matching) * BB84_CHECK_FRACTION)).
    """
    result = run_bb84(n_qubits=400, injected_noise=0.0, seed=42)
    matching = [
        i for i in range(len(result.alice_bits))
        if result.alice_bases[i] == result.bob_bases[i]
    ]
    expected_check = max(1, int(len(matching) * BB84_CHECK_FRACTION))
    assert result.qber_sample_size == expected_check, (
        f"qber_sample_size={result.qber_sample_size} but expected {expected_check} "
        f"(matching={len(matching)}, BB84_CHECK_FRACTION={BB84_CHECK_FRACTION})"
    )
    print(f"\n[check_sample] matching={len(matching)}  "
          f"check={result.qber_sample_size}  key={len(result.sifted_key)}  "
          f"fraction={BB84_CHECK_FRACTION}")


def test_check_fraction_parameter_overrides_default():
    """
    Passing check_fraction explicitly overrides BB84_CHECK_FRACTION.
    A smaller check_fraction leaves more bits as key material.
    """
    n_qubits = 400
    seed = 10
    result_default = run_bb84(n_qubits=n_qubits, injected_noise=0.0, seed=seed)
    result_narrow  = run_bb84(n_qubits=n_qubits, injected_noise=0.0, seed=seed,
                               check_fraction=0.30)
    # Narrower check → smaller check sample, larger key
    assert result_narrow.qber_sample_size < result_default.qber_sample_size, (
        "check_fraction=0.30 should yield a smaller check sample than default"
    )
    assert len(result_narrow.sifted_key) > len(result_default.sifted_key), (
        "check_fraction=0.30 should yield a larger sifted key than default"
    )
    print(f"\n[check_fraction_param]"
          f"  default check={result_default.qber_sample_size} key={len(result_default.sifted_key)}"
          f"  narrow  check={result_narrow.qber_sample_size}  key={len(result_narrow.sifted_key)}")
