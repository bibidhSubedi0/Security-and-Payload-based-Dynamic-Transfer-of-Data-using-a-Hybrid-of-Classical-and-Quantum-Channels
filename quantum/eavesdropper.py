"""
BB84 Intercept-Resend Eavesdropper Simulation
==============================================

Simulates the canonical BB84 intercept-resend attack (Eve) for security analysis
and QBER threshold validation.

-------------
Attack Theory
-------------
Eve performs intercept-resend on each qubit independently:

1. Alice sends qubit prepared in basis ab in {Z, X} encoding bit a.
2. Eve intercepts, picks random basis eb in {Z, X}, measures -> records result e.
3. Eve resends a freshly prepared qubit: bit=e in basis=eb.
4. Bob measures in his own random basis bb -> records result b.

QBER Analysis on Sifted Key (positions where ab == bb):

Case 1: eb == ab (probability 0.5)
  Eve gets the correct bit; she sends the same state Alice sent.
  When ab == bb the result is deterministic -> no error.
  Contribution to QBER: 0.

Case 2: eb != ab (probability 0.5)
  Eve measures in wrong basis. Eve gets a random bit; her resent
  qubit is in the wrong basis for Alice encoding.
  When Bob measures in bb == ab ( != eb), outcome is uniformly random.
  Error probability in this sub-case: 0.5.
  Contribution to QBER: 0.5 * 0.5 = 0.25.

Expected QBER approx 0.25 (25 percent) -- reliably above the 0.11
session-abort threshold (BB84_QBER_THRESHOLD).

Practical range: 15-35 percent per run (shot noise on ~100 check-sample bits).
With n_qubits=200: ~50 check bits, sigma(QBER) approx sqrt(0.25*0.75/50) approx 6 percent.

--------------------
Implementation Notes
--------------------
Two Qiskit circuit runs per qubit (not one), to correctly simulate the
state collapse at Eve measurement boundary:

  Circuit A: Alice prepared state -> Eve measures in eb -> eve_result
  Circuit B: Eve re-prepares (eve_result, eb) -> Bob measures in bb -> bob_result

This is the only correct way to model collapse-and-resend without
using mid-circuit measurement or density-matrix simulation.

This module does NOT modify quantum/bb84.py. It reuses _prepare_qubit
and _measure_qubit from that module as building blocks.

----------
Public API
----------
run_bb84_with_eve(n_qubits=200, seed=None) -> EveInterceptResult

---------------
Data Structures
---------------
EveInterceptResult (dataclass):
    alice_bits:       list[int]   Alice's original random bits (length n_qubits)
    alice_bases:      list[int]   Alice's bases: 0=Z, 1=X (length n_qubits)
    eve_bases:        list[int]   Eve's random basis choices (length n_qubits)
    eve_results:      list[int]   Eve's measurement outcomes (length n_qubits)
    bob_bases:        list[int]   Bob's random basis choices (length n_qubits)
    bob_results:      list[int]   Bob's outcomes from Eve's resent qubits (length n_qubits)
    sifted_key:       list[int]   Alice's bits at matching alice_basis==bob_basis positions (after check split)
    qber:             float       Measured QBER on check sample
    qber_sample_size: int         Number of check bits used
    exceeds_abort_threshold: bool Property, True if qber > BB84_QBER_THRESHOLD

-----------
Integration
-----------
Used by:
  - tests/test_eavesdropper.py: validates QBER ~0.25, threshold exceeded
  - scripts/bench_qkd.py: benchmark intercept-resend QBER distribution
  - Security analysis: confirms BB84_QBER_THRESHOLD=0.11 catches this attack
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from qiskit_aer import AerSimulator

from quantum.bb84 import _prepare_qubit, _measure_qubit, BB84_QBER_THRESHOLD

__all__ = ["EveInterceptResult", "run_bb84_with_eve"]


@dataclass
class EveInterceptResult:
    """
    Output of run_bb84_with_eve(). Includes Eve's measurement data.

    ----------
    Attributes
    ----------
    alice_bits : list[int]
        Alice's random bit string (length n_qubits). Same as BB84Result.alice_bits.
    alice_bases : list[int]
        Alice's basis choices per qubit: 0=Z, 1=X (length n_qubits).
    eve_bases : list[int]
        Eve's random basis choices per qubit: 0=Z, 1=X (length n_qubits).
        Independent of Alice and Bob.
    eve_results : list[int]
        Eve's measurement outcomes (0 or 1) for each qubit (length n_qubits).
        When eve_bases[i] == alice_bases[i], eve_results[i] == alice_bits[i].
        When eve_bases[i] != alice_bases[i], eve_results[i] is random.
    bob_bases : list[int]
        Bob's random basis choices per qubit: 0=Z, 1=X (length n_qubits).
        Independent of Alice and Eve.
    bob_results : list[int]
        Bob's measurement outcomes from Eve's resent qubits (length n_qubits).
        These are what Bob would see under active intercept-resend attack.
    sifted_key : list[int]
        Alice's raw key bits at positions where alice_basis == bob_basis,
        after sacrificing check_fraction for QBER estimation.
        This is what would be passed to reconciliation if QBER < threshold.
    qber : float
        Quantum Bit Error Rate measured on the check sample (sacrificed bits).
        Expected ~0.25 for intercept-resend. Range [0, 1].
    qber_sample_size : int
        Number of sifted bits used for QBER estimation.
    exceeds_abort_threshold : bool (property)
        True if qber > BB84_QBER_THRESHOLD (0.11). Indicates session would abort.
        For intercept-resend, this should be True in vast majority of runs.
    """
    alice_bits:       list[int]
    alice_bases:      list[int]   # 0=Z, 1=X
    eve_bases:        list[int]   # Eve's random basis choices
    eve_results:      list[int]   # what Eve measured
    bob_bases:        list[int]
    bob_results:      list[int]   # what Bob measured (from Eve's resent qubit)
    sifted_key:       list[int]   # Alice's bits at matching alice_basis==bob_basis positions
    qber:             float
    qber_sample_size: int

    @property
    def exceeds_abort_threshold(self) -> bool:
        """True if QBER > BB84_QBER_THRESHOLD (would trigger Phase 2 abort)."""
        return self.qber > BB84_QBER_THRESHOLD


def run_bb84_with_eve(
    n_qubits: int = 200,
    seed: int | None = None,
) -> EveInterceptResult:
    """
    Simulate BB84 with Eve performing a full intercept-resend attack.

    ----------
    Parameters
    ----------
    n_qubits : int, default 200
        Number of qubits Alice transmits. Same as run_bb84 default.
    seed : int or None, default None
        RNG seed for reproducibility. Seeds Python's random module.
        Controls Alice/Eve/Bob bit and basis choices.

    -------
    Returns
    -------
    EveInterceptResult
        Contains all intermediate measurement data plus QBER and sifted_key.
        Expected QBER ~0.25. exceeds_abort_threshold property indicates
        whether the attack would be detected (should be True).

    -------------------------
    Protocol Steps (Internal)
    -------------------------
    1. Generate random bits and bases for Alice, Eve, Bob (all length n_qubits).
    2. For each qubit i:
         Circuit A (Alice -> Eve):
           - Alice prepares via _prepare_qubit(alice_bits[i], alice_bases[i])
           - Eve measures via _measure_qubit(qc, eve_bases[i])
           - Single-shot simulation -> eve_results[i]
         Circuit B (Eve -> Bob):
           - Eve re-prepares via _prepare_qubit(eve_results[i], eve_bases[i])
           - Bob measures via _measure_qubit(qc, bob_bases[i])
           - Single-shot simulation -> bob_results[i]
       This two-circuit approach correctly models state collapse at Eve's
       measurement. A single circuit with mid-circuit measure would give
       wrong statistics (no collapse of Eve's qubit before resend).
    3. Sifting: keep positions where alice_bases[i] == bob_bases[i].
    4. Partition sifted indices: first half -> check sample, second half -> key.
    5. QBER = errors / check_sample_size on check positions.
    6. sifted_key = alice_bits[key_positions].

    --------------------------
    Why Two Circuits Per Qubit
    --------------------------
    In a real intercept-resend attack, Eve's measurement collapses the
    quantum state. The qubit she resends is a fresh preparation based on
    her measurement outcome. Simulating this requires:
      - Circuit A: Alice's state evolves to Eve's measurement (collapse)
      - Circuit B: New independent circuit with Eve's prepared state
    A single circuit with two measurements would not correctly model the
    fact that Eve's measurement destroys the original superposition.

    -----------
    Integration
    -----------
    Reuses _prepare_qubit and _measure_qubit from quantum.bb84 as
    building blocks. Does not import run_bb84 (avoids circular dependency
    and keeps attack simulation independent).
    """
    rng = random.Random(seed)
    simulator = AerSimulator(seed_simulator=seed)

    alice_bits  = [rng.randint(0, 1) for _ in range(n_qubits)]
    alice_bases = [rng.randint(0, 1) for _ in range(n_qubits)]
    eve_bases   = [rng.randint(0, 1) for _ in range(n_qubits)]
    bob_bases   = [rng.randint(0, 1) for _ in range(n_qubits)]

    eve_results: list[int] = []
    bob_results: list[int] = []

    for i in range(n_qubits):
        # Circuit A: Alice prepares -> Eve intercepts and measures
        qc_alice = _prepare_qubit(alice_bits[i], alice_bases[i])
        _measure_qubit(qc_alice, eve_bases[i])
        job = simulator.run(qc_alice, shots=1)
        eve_result = int(list(job.result().get_counts().keys())[0])
        eve_results.append(eve_result)

        # Circuit B: Eve re-prepares fresh qubit -> Bob measures
        qc_eve = _prepare_qubit(eve_result, eve_bases[i])
        _measure_qubit(qc_eve, bob_bases[i])
        job = simulator.run(qc_eve, shots=1)
        bob_result = int(list(job.result().get_counts().keys())[0])
        bob_results.append(bob_result)

    # Sifting: positions where Alice's basis matches Bob's basis
    matching = [
        i for i in range(n_qubits) if alice_bases[i] == bob_bases[i]
    ]

    if len(matching) < 4:
        return EveInterceptResult(
            alice_bits, alice_bases, eve_bases, eve_results,
            bob_bases, bob_results, [], 0.0, 0,
        )

    # First half -> QBER check; second half -> sifted key
    split           = max(1, len(matching) // 2)
    check_positions = matching[:split]
    key_positions   = matching[split:]

    errors = sum(
        1 for i in check_positions if alice_bits[i] != bob_results[i]
    )
    qber = errors / len(check_positions) if check_positions else 0.0

    sifted_key = [alice_bits[i] for i in key_positions]

    return EveInterceptResult(
        alice_bits=alice_bits,
        alice_bases=alice_bases,
        eve_bases=eve_bases,
        eve_results=eve_results,
        bob_bases=bob_bases,
        bob_results=bob_results,
        sifted_key=sifted_key,
        qber=qber,
        qber_sample_size=len(check_positions),
    )