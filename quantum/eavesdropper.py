"""
BB84 intercept-resend eavesdropper — Phase 8 Part B

Theory
------
Eve performs the canonical BB84 intercept-resend attack:

  Alice sends qubit prepared in basis ab ∈ {Z, X} encoding bit a.
  Eve intercepts, picks a random basis eb, measures → records result e.
  Eve resends a freshly prepared qubit: bit=e in basis=eb.
  Bob measures in his own random basis bb → records result b.

QBER analysis on the sifted key (positions where ab == bb):

  Case 1: eb == ab  (P = 0.5)
    Eve gets the right bit; she sends the same state Alice sent.
    When ab == bb the result is deterministic → no error.
    Contribution to QBER: 0.

  Case 2: eb != ab  (P = 0.5)
    Eve's state is the "wrong" basis.  Eve gets a random bit; her resent
    qubit is in the wrong basis for Alice's encoding.
    When Bob measures in bb == ab (≠ eb), the outcome is uniformly random.
    Error probability in this sub-case: 0.5.
    Contribution to QBER: 0.5 × 0.5 = 0.25.

Expected QBER ≈ 0.25 (25%) — reliably above the 0.11 session-abort threshold.

Practical range: 15–35% per run (shot noise on ~100 check-sample bits).
With n_qubits=200: ~50 check bits, σ(QBER) ≈ √(0.25×0.75/50) ≈ 6%.

Implementation
--------------
Two Qiskit circuit runs per qubit (not one), to correctly simulate the
state collapse at Eve's measurement boundary:
  Circuit A: Alice's prepared state → Eve measures in eb → eve_result
  Circuit B: Eve re-prepares (eve_result, eb) → Bob measures in bb → bob_result

This is the only correct way to model the collapse-and-resend without
using mid-circuit measurement or density-matrix simulation.

This module does NOT modify quantum/bb84.py.  It reuses _prepare_qubit
and _measure_qubit from that module as building blocks.
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
    """Output of run_bb84_with_eve().  Includes Eve's measurement data."""
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

    Parameters
    ----------
    n_qubits : Number of qubits Alice transmits.
    seed     : RNG seed for reproducibility.

    Returns
    -------
    EveInterceptResult with measured QBER (expected ~0.25) and all intermediate
    measurement data for inspection and verification.
    """
    rng = random.Random(seed)
    simulator = AerSimulator()

    alice_bits  = [rng.randint(0, 1) for _ in range(n_qubits)]
    alice_bases = [rng.randint(0, 1) for _ in range(n_qubits)]
    eve_bases   = [rng.randint(0, 1) for _ in range(n_qubits)]
    bob_bases   = [rng.randint(0, 1) for _ in range(n_qubits)]

    eve_results: list[int] = []
    bob_results: list[int] = []

    for i in range(n_qubits):
        # ---------------------------------------------------------------
        # Circuit A: Alice prepares → Eve intercepts and measures
        # ---------------------------------------------------------------
        qc_alice = _prepare_qubit(alice_bits[i], alice_bases[i])
        _measure_qubit(qc_alice, eve_bases[i])
        job = simulator.run(qc_alice, shots=1)
        eve_result = int(list(job.result().get_counts().keys())[0])
        eve_results.append(eve_result)

        # ---------------------------------------------------------------
        # Circuit B: Eve re-prepares fresh qubit → Bob measures
        # ---------------------------------------------------------------
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

    # First half → QBER check; second half → sifted key
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
