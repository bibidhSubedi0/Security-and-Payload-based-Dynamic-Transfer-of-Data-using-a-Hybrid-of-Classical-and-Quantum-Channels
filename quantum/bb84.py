"""BB84 QKD protocol implemented with Qiskit."""

import random
from dataclasses import dataclass

import numpy as np
from qiskit import QuantumCircuit
from qiskit_aer import AerSimulator


@dataclass
class BB84Result:
    alice_bits: list[int]
    alice_bases: list[int]  # 0 = Z, 1 = X
    bob_bases: list[int]
    bob_results: list[int]
    sifted_key: list[int]
    qber: float
    qber_sample_size: int


BB84_QBER_THRESHOLD: float = 0.11   # ~11% — standard security threshold

# Fraction of sifted (matching-basis) bits sacrificed for the QBER check sample.
# The remaining (1 - BB84_CHECK_FRACTION) bits become raw key material.
#
# Larger values → more reliable QBER estimate at the cost of fewer key bits.
# Smaller values → more key bits but riskier QBER estimate (more false "secure").
#
# Must match the split used in server/ebit_server.py _simulate_qkd().
# Benchmark grid may override via run_bb84(check_fraction=...) for comparison runs.
BB84_CHECK_FRACTION: float = 0.75


def _prepare_qubit(bit: int, basis: int) -> QuantumCircuit:
    """Prepare one qubit: bit in Z-basis (0=|0>, 1=|1>) or X-basis (0=|+>, 1=|->)."""
    qc = QuantumCircuit(1, 1)
    if bit == 1:
        qc.x(0)
    if basis == 1:  # X basis
        qc.h(0)
    return qc


def _measure_qubit(qc: QuantumCircuit, basis: int) -> QuantumCircuit:
    """Append measurement in Z or X basis."""
    if basis == 1:
        qc.h(0)
    qc.measure(0, 0)
    return qc


def run_bb84(
    n_qubits: int = 200,
    injected_noise: float = 0.0,
    seed: int | None = None,
    check_fraction: float = BB84_CHECK_FRACTION,
) -> BB84Result:
    """
    Simulate BB84 between Alice and Bob.

    Parameters
    ----------
    n_qubits:       Number of qubits Alice sends.
    injected_noise: Probability that a transmitted bit is flipped (models Eve
                    or channel noise).  0.0 = noiseless.
    seed:           RNG seed for reproducibility.

    Returns
    -------
    BB84Result with sifted key and QBER computed on a sacrificed check subset.
    """
    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)

    simulator = AerSimulator()

    alice_bits = [rng.randint(0, 1) for _ in range(n_qubits)]
    alice_bases = [rng.randint(0, 1) for _ in range(n_qubits)]
    bob_bases = [rng.randint(0, 1) for _ in range(n_qubits)]

    bob_results: list[int] = []
    for i in range(n_qubits):
        qc = _prepare_qubit(alice_bits[i], alice_bases[i])
        # Basis-symmetric noise injection:
        #   X gate (bit-flip)   causes errors in Z-basis measurements; no effect
        #   in X-basis (X|+> = |+>, X|-> = -|-> — global phase only).
        #   Z gate (phase-flip) causes errors in X-basis measurements; no effect
        #   in Z-basis (Z|0> = |0>, Z|1> = -|1> — global phase on |1> only).
        # Applying both independently with probability `injected_noise` gives
        #   P(error in Z basis) = P(X applied) = injected_noise
        #   P(error in X basis) = P(Z applied) = injected_noise
        # so measured QBER tracks injected_noise 1:1 regardless of basis.
        if injected_noise > 0.0:
            if np_rng.random() < injected_noise:
                qc.x(0)   # bit-flip:   error in Z-basis, transparent in X-basis
            if np_rng.random() < injected_noise:
                qc.z(0)   # phase-flip: error in X-basis, transparent in Z-basis
        _measure_qubit(qc, bob_bases[i])
        job = simulator.run(qc, shots=1)
        counts = job.result().get_counts()
        bob_results.append(int(list(counts.keys())[0]))

    # Sifting: keep positions where bases agree
    matching = [
        i for i in range(n_qubits) if alice_bases[i] == bob_bases[i]
    ]

    if len(matching) < 4:
        return BB84Result(
            alice_bits, alice_bases, bob_bases, bob_results, [], 0.0, 0
        )

    # Sacrifice check_fraction of sifted bits for QBER estimation;
    # the remainder become the raw key material.
    split = max(1, int(len(matching) * check_fraction))
    check_positions = matching[:split]
    key_positions = matching[split:]

    errors = sum(
        1 for i in check_positions if alice_bits[i] != bob_results[i]
    )
    qber = errors / len(check_positions) if check_positions else 0.0

    sifted_key = [alice_bits[i] for i in key_positions]

    return BB84Result(
        alice_bits=alice_bits,
        alice_bases=alice_bases,
        bob_bases=bob_bases,
        bob_results=bob_results,
        sifted_key=sifted_key,
        qber=qber,
        qber_sample_size=len(check_positions),
    )
