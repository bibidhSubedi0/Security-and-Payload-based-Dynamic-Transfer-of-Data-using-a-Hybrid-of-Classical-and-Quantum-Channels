"""
BB84 Prepare-and-Measure Quantum Key Distribution
=================================================

The canonical BB84 protocol (Bennett & Brassard, 1984) is implemented using Qiskit AerSimulator for quantum circuit execution.

-----------------
Protocol Overview
-----------------
1. Alice generates random bits and random bases (Z or X) for each qubit.
2. Alice prepares each qubit: |0>/|1> in Z-basis, |+>/|-> in X-basis.
3. Bob measures each qubit in a random basis (Z or X).
4. Sifting: Alice and Bob publicly compare bases (over authenticated classical channel) and keep only bits where bases matched.
5. QBER Check: A fraction (BB84_CHECK_FRACTION = 0.75) of sifted bits are sacrificed to estimate Quantum Bit Error Rate. If QBER > BB84_QBER_THRESHOLD (0.11 ≈ 11%), the session is aborted since this exceeds the tolerable error rate for secure key extraction against general coherent attacks.
6. Remaining sifted bits become raw key material, passed to reconciliation (single-pass Cascade) then HKDF-SHA256 for final AES-256-GCM session key.

-----------
Noise Model
-----------
`injected_noise` parameter models symmetric depolarising noise:
- With probability p: apply X gate (bit-flip) → error in Z-basis, no effect in X-basis
- With probability p: apply Z gate (phase-flip) → error in X-basis, no effect in Z-basis
This yields measured QBER ≈ injected_noise regardless of basis choice, enabling direct comparison with theoretical thresholds.

---------
Constants
---------
BB84_QBER_THRESHOLD: float = 0.11
Security abort threshold. Theoretical unconditional security proof (Shor & Preskill) permits up to ~11% QBER against general coherent attacks. Exceeding this → session aborted, no key derived.

BB84_CHECK_FRACTION: float = 0.75
    Fraction of sifted bits sacrificed for QBER estimation. The remainder (25%) become raw key bits.
    Rationale: With n_qubits=200, ~50% basis match → ~100 sifted bits.
    75% check fraction → ~75 check bits, $\sigma$(QBER) ≈ √(p(1-p)/75).
    At p=0.11: $\sigma$ ≈ 3.6% → adequate to distinguish 11% from 25% (Eve).
    MUST match the split in server/ebit_server.py:_simulate_qkd().

---------------
Data Structures
---------------
BB84Result (dataclass):
    alice_bits:      List of Alice's original random bits (length n_qubits)
    alice_bases:     Alice's basis choices: 0=Z, 1=X (length n_qubits)
    bob_bases:       Bob's basis choices: 0=Z, 1=X (length n_qubits)
    bob_results:     Bob's measurement outcomes (length n_qubits)
    sifted_key:      Raw key bits after sifting & check-bit sacrifice (variable)
    qber:            Measured QBER on check sample (float in [0,1])
    qber_sample_size: Number of check bits used for QBER estimate (int)

---------
Functions
---------
_prepare_qubit(bit, basis) -> QuantumCircuit
    Internal helper: creates 1-qubit circuit encoding `bit` in `basis`.
    bit=0, basis=0 → |0>; bit=1, basis=0 → |1>; bit=0, basis=1 → |+>; bit=1, basis=1 → |->.
    Returns circuit WITHOUT measurement.

_measure_qubit(qc, basis) -> QuantumCircuit
    Internal helper: appends measurement in given basis to circuit `qc`.
    Z-basis (0): direct measure. X-basis (1): H then measure.
    Mutates and returns `qc`.

run_bb84(n_qubits=200, injected_noise=0.0, seed=None, check_fraction=BB84_CHECK_FRACTION) -> BB84Result
    Full BB84 simulation. Public API used by:
      - server/ebit_server.py:_simulate_qkd() for session key generation
      - quantum/eavesdropper.py:run_bb84_with_eve() as building block
      - tests/test_bb84.py for protocol verification
    Parameters:
      n_qubits:        Number of qubits Alice transmits (default 200)
      injected_noise:  Symmetric depolarising probability (default 0.0)
      seed:            RNG seed for reproducibility (Python + NumPy)
      check_fraction:  Override BB84_CHECK_FRACTION (for benchmarking only)
    Returns BB84Result with sifted_key ready for reconciliation.

--------------
Security Notes
--------------
- Assumes authenticated classical channel (prevents basis-comparison MITM).
- Device trust assumed: no device-independent security.
- QBER threshold 0.11 is conservative; practical implementations may use
  lower values (e.g., 0.08) for higher key rates with finite-size effects.
- Reconciliation + HKDF provides privacy amplification against information
  leaked during parity exchange.

-----------
Integration
-----------
Called by server/ebit_server.py during session establishment. The returned
sifted_key feeds into reconciliation.py (if QBER ≤ 0.11) then
classical.aes_channel.derive_key() for final AES-256-GCM key.
"""

import random
from dataclasses import dataclass

import numpy as np
from qiskit import QuantumCircuit
from qiskit_aer import AerSimulator


@dataclass
class BB84Result:
    """
    Result of a complete BB84 simulation run.

    ----------
    Attributes
    ----------
    alice_bits : list[int]
        Alice's random bit string (length n_qubits). These are the raw encoded bits
        before any sifting. Used by eavesdropper.py to compare Eve's measurements.
    alice_bases : list[int]
        Alice's basis choices per qubit: 0 = Z (computational), 1 = X (Hadamard).
        Must be compared with bob_bases for sifting. Length n_qubits.
    bob_bases : list[int]
        Bob's random basis choices per qubit: 0 = Z, 1 = X. Length n_qubits.
    bob_results : list[int]
        Bob's measurement outcomes (0 or 1) for each qubit. Length n_qubits.
        Errors vs alice_bits at matching-basis positions determine QBER.
    sifted_key : list[int]
        The surviving key material after sifting AND check-bit sacrifice.
        This is the raw key passed to reconciliation.py. Length varies;
        typically ≈ n_qubits * 0.5 * (1 - BB84_CHECK_FRACTION).
    qber : float
        Quantum Bit Error Rate measured on the check sample (sacrificed bits).
        Range [0, 1]. If > BB84_QBER_THRESHOLD (0.11), session should be aborted.
    qber_sample_size : int
        Number of sifted bits used for QBER estimation (the check sample).
        Determines statistical confidence: $\sigma$ ≈ √(qber*(1-qber)/qber_sample_size).
    """

    alice_bits: list[int]
    alice_bases: list[int]  # 0 = Z, 1 = X
    bob_bases: list[int]
    bob_results: list[int]
    sifted_key: list[int]
    qber: float
    qber_sample_size: int


# Security threshold: abort if measured QBER exceeds this value.
# 0.11 ≈ 11% is the Shor-Preskill bound for unconditional security against
# general coherent attacks. Intercept-resend (Eve) yields ~0.25 QBER, well above.
BB84_QBER_THRESHOLD: float = 0.11

# Fraction of sifted (matching-basis) bits sacrificed for QBER estimation.
# Remaining (1 - BB84_CHECK_FRACTION) become raw key material.
# 0.75 → 75% check, 25% key. With n_qubits=200: ~100 sifted → ~75 check, ~25 key.
# MUST match the split in server/ebit_server.py:_simulate_qkd().
# Benchmark grid (scripts/bench_qkd.py) may override via run_bb84(check_fraction=...).
BB84_CHECK_FRACTION: float = 0.75


def _prepare_qubit(bit: int, basis: int) -> QuantumCircuit:
    """
    Prepare a single qubit encoding `bit` in the specified `basis`.

    ----------
    Parameters
    ----------
    bit : int
        0 or 1 = the classical bit to encode.
    basis : int
        0 = Z-basis (computational): |0> for bit=0, |1> for bit=1.
        1 = X-basis (Hadamard):   |+> for bit=0, |-> for bit=1.

    -------
    Returns
    -------
    QuantumCircuit
        1-qubit, 1-classical-bit circuit with state preparation ONLY.
        Measurement is NOT included; caller must append via _measure_qubit().

    --------------------
    Circuit Construction
    --------------------
    Z-basis (basis=0):
        bit=0: Identity → |0>
        bit=1: X gate   → |1>
    X-basis (basis=1):
        bit=0: H gate   → |+> = (|0> + |1>)/√2
        bit=1: X then H → |-> = (|0> - |1>)/√2
    """
    qc = QuantumCircuit(1, 1)
    if bit == 1:
        qc.x(0)
    if basis == 1:  # X basis
        qc.h(0)
    return qc


def _measure_qubit(qc: QuantumCircuit, basis: int) -> QuantumCircuit:
    """
    Append measurement in the specified basis to an existing circuit.

    ----------
    Parameters
    ----------
    qc : QuantumCircuit
        Circuit with prepared qubit state (from _prepare_qubit).
    basis : int
        0 = Z-basis measurement (direct computational basis).
        1 = X-basis measurement (apply H before measure).

    -------
    Returns
    -------
    QuantumCircuit
        Same circuit `qc` with measurement appended (mutated in place).

    -----
    Notes
    -----
    X-basis measurement is implemented as H followed by Z-basis measure.
    This is equivalent to measuring in the |+>/|-> basis because H|+> = |0>,
    H|-> = |1>. The classical bit register captures the outcome.
    """
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
    Execute a complete BB84 protocol simulation.

    ----------
    Parameters
    ----------
    n_qubits : int, default 200
        Number of qubits Alice prepares and sends to Bob.
        Typical values: 200-1000. Larger → more key bits, better statistics.
    injected_noise : float, default 0.0
        Symmetric depolarising noise probability per qubit.
        Models channel noise OR eavesdropping (see quantum/eavesdropper.py
        for explicit intercept-resend attack).
        Mechanism: with prob p apply X (bit-flip), with prob p apply Z (phase-flip).
        Yields measured QBER ≈ injected_noise in both bases.
    seed : int or None, default None
        RNG seed for reproducibility. Seeds both Python's random and NumPy's
        default_rng. Same seed → identical bit/basis sequences and noise pattern.
    check_fraction : float, default BB84_CHECK_FRACTION (0.75)
        Fraction of sifted bits to sacrifice for QBER estimation.
        Override ONLY for benchmarking/comparison (scripts/bench_qkd.py).
        Production must use the default to match server/ebit_server.py.

    -------
    Returns
    -------
    BB84Result
        Contains all raw data plus sifted_key and qber.
        If QBER > BB84_QBER_THRESHOLD, caller MUST abort session (no key derived).
        If len(matching_bases) < 4: returns empty sifted_key, qber=0.0 (degenerate).

    -------------------------
    Protocol Steps (Internal)
    -------------------------
    1. Generate random bits & bases for Alice and Bob (length n_qubits).
    2. For each qubit i:
         - Alice prepares qubit via _prepare_qubit(alice_bits[i], alice_bases[i])
         - If injected_noise > 0: apply random X and/or Z with prob injected_noise
         - Bob measures via _measure_qubit(qc, bob_bases[i])
         - Single-shot simulation → bob_results[i]
    3. Sifting: keep indices where alice_bases[i] == bob_bases[i].
    4. Partition sifted indices: first check_fraction → check sample, rest → key.
    5. QBER = errors / check_sample_size on check positions.
    6. sifted_key = alice_bits[key_positions].

    -----------
    Integration
    -----------
    Primary caller: server/ebit_server.py:_simulate_qkd()
    Secondary: quantum/eavesdropper.py (reuses _prepare_qubit, _measure_qubit)
    Testing: tests/test_bb84.py validates QBER statistics, sifting correctness.
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
        #   in X-basis (X|+> = |+>, X|-> = -|->: global phase only).
        #   Z gate (phase-flip) causes errors in X-basis measurements; no effect
        #   in Z-basis (Z|0> = |0>, Z|1> = -|1>: global phase on |1> only).
        # Applying both independently with probability `injected_noise` gives
        #   P(error in Z basis) = P(X applied) = injected_noise
        #   P(error in X basis) = P(Z applied) = injected_noise
        # so measured QBER tracks injected_noise 1:1 regardless of basis.
        if injected_noise > 0.0:
            if np_rng.random() < injected_noise:
                qc.x(0)  # bit-flip:   error in Z-basis, transparent in X-basis
            if np_rng.random() < injected_noise:
                qc.z(0)  # phase-flip: error in X-basis, transparent in Z-basis
        _measure_qubit(qc, bob_bases[i])
        job = simulator.run(qc, shots=1)
        counts = job.result().get_counts()
        bob_results.append(int(list(counts.keys())[0]))

    # Sifting: keep positions where bases agree
    matching = [i for i in range(n_qubits) if alice_bases[i] == bob_bases[i]]

    if len(matching) < 4:
        return BB84Result(alice_bits, alice_bases, bob_bases, bob_results, [], 0.0, 0)

    # Sacrifice check_fraction of sifted bits for QBER estimation;
    # the remainder become the raw key material.
    split = max(1, int(len(matching) * check_fraction))
    check_positions = matching[:split]
    key_positions = matching[split:]

    errors = sum(1 for i in check_positions if alice_bits[i] != bob_results[i])
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
