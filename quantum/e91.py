"""E91 entanglement-based QKD with CHSH inequality test."""

import random
from dataclasses import dataclass

import numpy as np
from qiskit import QuantumCircuit
from qiskit_aer import AerSimulator


# Alice measurement angles (radians): a=0, a'=π/4
# Bob measurement angles:             b=π/8, b'=3π/8
# These maximise CHSH violation for a singlet state.
ALICE_ANGLES = [0.0, np.pi / 4]          # a, a'
BOB_ANGLES   = [np.pi / 8, 3 * np.pi / 8]  # b, b'

ANGLE_PAIRS_FOR_CHSH = [
    (0, 0),   # (a,  b)  → E(a,b)
    (0, 1),   # (a,  b') → E(a,b')
    (1, 0),   # (a', b)  → E(a',b)
    (1, 1),   # (a', b') → E(a',b')
]


@dataclass
class E91Result:
    chsh_s: float
    correlations: dict[str, float]   # key: "E(ai,bj)"
    exceeds_classical: bool           # |S| > 2
    n_pairs_per_setting: int


def _make_singlet() -> QuantumCircuit:
    """Create |Φ+> Bell state: (|00> + |11>) / √2."""
    qc = QuantumCircuit(2, 2)
    qc.h(0)
    qc.cx(0, 1)
    return qc


def _measure_at_angle(qc: QuantumCircuit, qubit: int, angle: float, cbit: int) -> None:
    """Rotate qubit so that measuring in Z basis is equivalent to angle basis."""
    qc.ry(-2 * angle, qubit)
    qc.measure(qubit, cbit)


def _run_correlation(
    alice_angle: float,
    bob_angle: float,
    n_shots: int,
    simulator: AerSimulator,
) -> float:
    """
    Estimate E(a,b) = <A⊗B> for one angle pair.
    +1 if both same, -1 if different; average over shots.
    """
    qc = _make_singlet()
    _measure_at_angle(qc, 0, alice_angle, 0)
    _measure_at_angle(qc, 1, bob_angle, 1)

    job = simulator.run(qc, shots=n_shots)
    counts = job.result().get_counts()

    expectation = 0.0
    for outcome, count in counts.items():
        # Qiskit returns bit-string as "b1 b0" with qubit 0 rightmost
        bits = outcome.replace(" ", "")
        a_bit = int(bits[-1])   # qubit 0 = Alice
        b_bit = int(bits[-2])   # qubit 1 = Bob
        sign = +1 if a_bit == b_bit else -1
        expectation += sign * count
    return expectation / n_shots


def run_e91(
    n_pairs_per_setting: int = 500,
    seed: int | None = None,
) -> E91Result:
    """
    Simulate E91 and compute CHSH parameter S.

    S = E(a,b) - E(a,b') + E(a',b) + E(a',b')

    Quantum bound: |S| ≤ 2√2 ≈ 2.828
    Classical bound: |S| ≤ 2
    """
    simulator = AerSimulator(seed_simulator=seed)

    correlations: dict[str, float] = {}
    e = {}
    for ai, bi in ANGLE_PAIRS_FOR_CHSH:
        a_label = "a'" if ai else "a"
        b_label = "b'" if bi else "b"
        key = f"E({a_label},{b_label})"
        val = _run_correlation(
            ALICE_ANGLES[ai], BOB_ANGLES[bi], n_pairs_per_setting, simulator
        )
        correlations[key] = round(val, 4)
        e[(ai, bi)] = val

    # S = E(a,b) - E(a,b') + E(a',b) + E(a',b')
    chsh_s = (
        e[(0, 0)]
        - e[(0, 1)]
        + e[(1, 0)]
        + e[(1, 1)]
    )

    return E91Result(
        chsh_s=round(chsh_s, 4),
        correlations=correlations,
        exceeds_classical=abs(chsh_s) > 2.0,
        n_pairs_per_setting=n_pairs_per_setting,
    )
