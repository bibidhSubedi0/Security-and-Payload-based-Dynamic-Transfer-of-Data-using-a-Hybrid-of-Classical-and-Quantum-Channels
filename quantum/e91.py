r"""
E91 Entanglement-Based QKD with CHSH Bell Test
===============================================

Implements the E91 protocol (Ekert, 1991) using shared entanglement and CHSH inequality violation to verify quantum channel security.

-----------------
Protocol Overview
-----------------
1. A source distributes singlet Bell pairs (|$\phi$+> = (|00> + |11>)/$\sqrt(2)$) to Alice and Bob (one qubit each).
2. Alice randomly chooses measurement basis a or a' (angles 0, $\pi$/4).
3. Bob randomly chooses measurement basis b or b' (angles $\pi$/8, 3$\pi$/8).
4. They repeat for n_pairs_per_setting pairs per angle combination.
5. CHSH parameter S = E(a,b) - E(a,b') + E(a',b) + E(a',b') is computed.
6. Quantum mechanics predicts |S| = 2*$\sqrt(2)$ approx 2.828.
   Classical local hidden variable theories bound |S| <= 2.
7. If |S| > 2, the channel exhibits genuine entanglement (no local hidden variable explanation). This certifies the key bits generated.

------------------
Measurement Angles
------------------
ALICE_ANGLES = [0.0, $\pi$/4]          # a, a'
BOB_ANGLES   = [$\pi$/8, 3*$\pi$/8]       # b, b'
These four angle pairs maximize CHSH violation for a singlet state.

ANGLE_PAIRS_FOR_CHSH = [(0,0), (0,1), (1,0), (1,1)]
Maps to correlations E(a,b), E(a,b'), E(a',b), E(a',b').

---------------
Data Structures
---------------
E91Result (dataclass):
    chsh_s: float
        Computed CHSH parameter S. Quantum bound: |S| <= 2*$\sqrt(2)$ approx 2.828.
        Classical bound: |S| <= 2. If |S| > 2, entanglement is verified.
    correlations: dict[str, float]
        Four correlation values: E(a,b), E(a,b'), E(a',b), E(a',b').
        Each in range [-1, 1]. Positive means correlated, negative anti-correlated.
    exceeds_classical: bool
        True if |chsh_s| > 2.0. Indicates Bell inequality violation.
    n_pairs_per_setting: int
        Number of entangled pairs measured per angle combination.
        Total pairs consumed = 4 * n_pairs_per_setting.

---------
Functions
---------
_make_singlet() -> QuantumCircuit
    Creates 2-qubit circuit preparing |Phi+> Bell state.
    Circuit: H on q0, CNOT q0->q1. No measurement appended.

_measure_at_angle(qc, qubit, angle, cbit) -> None
    Mutates circuit: applies Ry(-2*angle) then measures qubit into cbit.
    Ry(-2*$\theta$) rotates measurement basis from Z to angle $\theta$ in X-Z plane.
    Equivalent to measuring in cos($\theta$)|0> + sin($\theta$)|1> basis.

_run_correlation(alice_angle, bob_angle, n_shots, simulator) -> float
    Estimates E(a,b) = <A x B> for one angle pair.
    +1 if Alice and Bob outcomes match, -1 if different. Average over shots.

run_e91(n_pairs_per_setting=500, seed=None) -> E91Result
    Full E91 simulation with CHSH test.
    Parameters:
      n_pairs_per_setting: Pairs per angle combination (default 500).
                           Total circuit runs = 4 * n_pairs_per_setting.
      seed: RNG seed for AerSimulator (reproducible shot noise).
    Returns E91Result with chsh_s, correlations, exceeds_classical flag.
    Used by server/ebit_server.py for auxiliary entanglement verification.

--------------
Security Notes
--------------
- Device-independent in principle: CHSH violation certifies entanglement
  without trusting measurement devices (requires loophole-free setup).
- In this simulation: trusted devices, no detection/loophole modeling.
- CHSH violation (|S| > 2) implies no local eavesdropper can have full
  knowledge of outcomes. Key rate positive when |S| > 2.
- Practical QKD would extract key from correlated outcomes at specific
  angle pairs (e.g., a/b where correlation is maximal). This module only
  computes the Bell test; key extraction is out of scope.

-----------
Integration
-----------
Called by server/ebit_server.py alongside BB84 for defense-in-depth:
BB84 provides primary session key; E91 provides independent entanglement
verification on a parallel quantum channel. Both must pass for session
to proceed (policy defined in server logic).
"""

import random
from dataclasses import dataclass

import numpy as np
from qiskit import QuantumCircuit
from qiskit_aer import AerSimulator

# Alice measurement angles (radians): a=0, a'=pi/4
# Bob measurement angles:             b=pi/8, b'=3pi/8
# These maximize CHSH violation for a singlet state.
ALICE_ANGLES = [0.0, np.pi / 4]  # a, a'
BOB_ANGLES = [np.pi / 8, 3 * np.pi / 8]  # b, b'

ANGLE_PAIRS_FOR_CHSH = [
    (0, 0),  # (a,  b)  -> E(a,b)
    (0, 1),  # (a,  b') -> E(a,b')
    (1, 0),  # (a', b)  -> E(a',b)
    (1, 1),  # (a', b') -> E(a',b')
]


@dataclass
class E91Result:
    r"""
    Result of E91 CHSH Bell test.

    ----------
    Attributes
    ----------
    chsh_s : float
        CHSH parameter S = E(a,b) - E(a,b') + E(a',b) + E(a',b').
        Quantum bound: |S| <= 2*$\sqrt(2)$ approx 2.828.
        Classical bound: |S| <= 2.
        Rounded to 4 decimal places.
    correlations : dict[str, float]
        Dictionary with four keys: "E(a,b)", "E(a,b')", "E(a',b)", "E(a',b')".
        Values are expectation values in [-1, 1]. Rounded to 4 decimal places.
    exceeds_classical : bool
        True if |chsh_s| > 2.0. Indicates Bell inequality violation.
        False means results compatible with local hidden variable theory.
    n_pairs_per_setting : int
        Number of entangled pairs measured for each of the four angle settings.
        Total pairs = 4 * n_pairs_per_setting.
    """

    chsh_s: float
    correlations: dict[str, float]  # key: "E(ai,bj)"
    exceeds_classical: bool  # |S| > 2
    n_pairs_per_setting: int


def _make_singlet() -> QuantumCircuit:
    r"""
    Create |$\phi$+> Bell state: (|00> + |11>) / $\sqrt(2)$.

    -------
    Returns
    -------
    QuantumCircuit
        2-qubit, 2-classical-bit circuit with Bell state preparation only.
        Qubit 0 = Alice, Qubit 1 = Bob.
        Circuit: H(0), CX(0,1). No measurements appended.
    """
    qc = QuantumCircuit(2, 2)
    qc.h(0)
    qc.cx(0, 1)
    return qc


def _measure_at_angle(qc: QuantumCircuit, qubit: int, angle: float, cbit: int) -> None:
    r"""
    Rotate qubit so that Z-basis measurement equals measurement at angle.

    ----------
    Parameters
    ----------
    qc : QuantumCircuit
        Circuit containing the qubit to measure (mutated in place).
    qubit : int
        Qubit index (0 for Alice, 1 for Bob).
    angle : float
        Measurement angle in radians in the X-Z plane.
        0 = Z basis, $\pi$/2 = X basis, $\pi$/4 = diagonal.
    cbit : int
        Classical bit index to store measurement result.

    -----
    Notes
    -----
    Ry(-2*$\theta$) transforms the measurement basis:
    |0> -> cos($\theta$)|0> + sin($\theta$)|1>
    |1> -> -sin($\theta$)|0> + cos($\theta$)|1>
    Measuring in Z after this rotation is equivalent to measuring in the
    rotated basis. This is standard Qiskit technique for arbitrary basis measurement.
    """
    qc.ry(-2 * angle, qubit)
    qc.measure(qubit, cbit)


def _run_correlation(
    alice_angle: float,
    bob_angle: float,
    n_shots: int,
    simulator: AerSimulator,
) -> float:
    r"""
    Estimate E(a,b) = <A x B> for one angle pair.

    ----------
    Parameters
    ----------
    alice_angle : float
        Alice's measurement angle (radians).
    bob_angle : float
        Bob's measurement angle (radians).
    n_shots : int
        Number of circuit executions (shots).
    simulator : AerSimulator
        Qiskit Aer simulator instance.

    -------
    Returns
    -------
    float
        Correlation expectation value in [-1, 1].
        +1: perfect correlation (same outcomes).
        -1: perfect anti-correlation (opposite outcomes).
        0: uncorrelated.

    ---------
    Algorithm
    ---------
    For each shot:
      - Prepare fresh singlet |Phi+>
      - Alice measures at alice_angle -> outcome a in {0,1}
      - Bob measures at bob_angle -> outcome b in {0,1}
      - Sign = +1 if a == b, -1 if a != b
    Expectation = average of signs over all shots.
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
        a_bit = int(bits[-1])  # qubit 0 = Alice
        b_bit = int(bits[-2])  # qubit 1 = Bob
        sign = +1 if a_bit == b_bit else -1
        expectation += sign * count
    return expectation / n_shots


def run_e91(
    n_pairs_per_setting: int = 500,
    seed: int | None = None,
) -> E91Result:
    r"""
    Simulate E91 and compute CHSH parameter S.

    ----------
    Parameters
    ----------
    n_pairs_per_setting : int, default 500
        Number of entangled pairs per angle combination.
        Total circuit runs = 4 * n_pairs_per_setting.
        Larger values reduce statistical uncertainty in S.
    seed : int or None, default None
        Seed for AerSimulator. Controls shot noise reproducibility.

    -------
    Returns
    -------
    E91Result
        Contains chsh_s, four correlations, exceeds_classical flag.
        If exceeds_classical is True, |S| > 2 and entanglement is verified.

    ------------
    CHSH Formula
    ------------
    S = E(a,b) - E(a,b') + E(a',b) + E(a',b')

    Quantum bound: |S| <= 2*$\sqrt(2)$ approx 2.828
    Classical bound: |S| <= 2
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
    chsh_s = e[(0, 0)] - e[(0, 1)] + e[(1, 0)] + e[(1, 1)]

    return E91Result(
        chsh_s=round(chsh_s, 4),
        correlations=correlations,
        exceeds_classical=abs(chsh_s) > 2.0,
        n_pairs_per_setting=n_pairs_per_setting,
    )
