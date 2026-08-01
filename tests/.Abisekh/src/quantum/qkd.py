"""
Quantum Key Distribution (QKD) engine: BB84 and E91 protocol simulation.

======================
WHY THIS MODULE EXISTS
======================
This is the "QKD Engine" module of the hybrid quantum-classical system proposed in the project (Section 6.2.3). Its job is to securely establish a shared secret key between two parties (Node A / Alice and Node B / Bob) using quantum mechanics. The derived key is later used to AES-256-encrypt the classical payload. It must report the Quantum Bit Error Rate (QBER) after every run (to feed the Dynamic Split Controller), and additionally the CHSH parameter when E91 is selected, per the proposal's evaluation metrics (Section 6.8).

[!NOTE] CHSH parameter might exceed Tsirelson bound (2.828) in some runs due to statistical fluctuations and noises; this is expected and not a bug.

=================
WHAT IT SIMULATES
=================
- A quantum channel between Alice and Bob (implemented with Qiskit + AerSimulator).
- Optional channel noise (modelled as random bit flips at rate `noise`), which mimics real-world channel attenuation/decoherence and lets us see how QBER rises.
- An optional eavesdropper (intercept-resend attack): Eve measures the qubits in a random basis and resends a freshly prepared state. Any interception disturbs the quantum states, which MUST show up as elevated QBER (and, for E91, a weakened CHSH violation), which is the whole security premise of QKD.

=============
KEY DATA FLOW
=============
1. Alice picks random secret bits + random encoding bases (BB84) or measurement angles (E91).
2. Quantum states travel through the (simulated) channel, possibly disturbed by noise and/or Eve.
3. Bob measures in random bases; both parties publicly reveal ONLY their bases (sifting), keeping bits where bases matched -> shared raw key.
4. QBER = fraction of mismatched key bits (should be ~0 clean, >11% -> key is insecure per BB84 threshold and must be discarded/aborted). (Section 6.8.1)
5. E91 additionally runs a Bell (CHSH) test on a subset of pairs to certify the channel is genuinely quantum: |S| > 2 proves entanglement, |S| > 2.828 impossible.

[!INFO] For more details on documentation of the qiskit and qiskit_aer packages, please refer to the following links:
- Qiskit: https://quantum.cloud.ibm.com/docs/en/guides/tools-intro
- Qiskit Aer: https://qiskit.github.io/qiskit-aer/
"""

import numpy as np
from qiskit import QuantumCircuit
from qiskit_aer import AerSimulator

# =========
# Constants
# =========

# Basis identifiers for BB84. We encode each bit in one of two conjugate bases:
#     Z (computational) or X (Hadamard). Alice and Bob both draw bases from this set.
# WHY: Using non-orthogonal bases is what makes QKD secure. Eve can't measure both bases simultaneously (Heisenberg uncertainty), so any wrong-basis guess disturbs the state and inflates QBER.
Z_BASIS = 0
X_BASIS = 1

# Measurement angles (radians) available to Alice and Bob in E91.
# Index into these arrays: alice_angles[i] in {0,1,2,3} picks the angle used for pair i. Angles 0 and pi/4 are the KEY bases (matching -> key bit); angles pi/8 and 3*pi/8 are the TEST bases used only for the CHSH Bell test.
# WHY: The CHSH protocol needs specific angle combinations to reach the quantum maximum S = 2*sqrt(2) ~ 2.828 while capping classical correlations at S <= 2.
ALICE_BASES = np.array([0.0, np.pi / 4, np.pi / 8, 3 * np.pi / 8])
BOB_BASES = np.array([0.0, np.pi / 4, np.pi / 8, 3 * np.pi / 8])

# The four (Alice-angle, Bob-angle) combinations used to estimate the CHSH value. For a shared |phi+> Bell pair, the correlation E(a,b) = cos(2(a-b)), giving:
# S = E(0,pi/8) - E(0,3pi/8) + E(pi/4,pi/8) + E(pi/4,3pi/8) = 2*sqrt(2).
# The minus sign on the second pair is the standard CHSH alternating-sign form.
CHSH_PAIRS = [
    (0.0, np.pi / 8),
    (0.0, 3 * np.pi / 8),
    (np.pi / 4, np.pi / 8),
    (np.pi / 4, 3 * np.pi / 8),
]


# ===================================
# QKDEngine: the main protocol runner
# ===================================


class QKDEngine:
    """
    One-shot QKD engine supporting both BB84 (prepare-and-measure) and E91 (entanglement-based) protocols.

    **WHY A CLASS INSTANCE**: The engine holds shared simulation state (the RNG stream, the AerSimulator, and the user's noise/eavesdropper settings) so multiple runs are reproducible and the same channel conditions apply to every qubit/pair in a run.
    """

    def __init__(self, protocol="BB84", noise=0.0, eavesdropper=False, seed=None):
        """
        Args:
            protocol:     "BB84" or "E91": The protocol is user-selectable at runtime.
            noise:        Probability [0,1) of a bit flip on the channel, used to simulate channel degradation and drive up QBER.
            eavesdropper: If True, insert Eve (intercept-resend) into the channel.
            seed:         RNG seed for reproducible simulation runs (important for testing and for comparing scenarios fairly).

        **WHY**: Every knob in the proposal's evaluation framework (noise levels, eavesdropping detection, protocol choice) is exposed here.
        """
        if protocol not in ("BB84", "E91"):
            raise ValueError("Protocol must be 'BB84' or 'E91'.")
        self.protocol = protocol
        self.noise = noise
        self.eavesdropper = eavesdropper
        self.rng = np.random.default_rng(seed)
        self.simulator = AerSimulator()

    # =======
    # Helpers
    # =======

    def _batch_measure(self, circuits):
        """
        Run many independent circuits in ONE simulator call and return their per-circuit counts lists.

        **WHY**: Running circuits one-by-one is ~100x slower; batching is the key performance trick that lets us simulate 10^4-10^5 qubits/pairs quickly (needed for statistically stable QBER/CHSH estimates).
        """
        result = self.simulator.run(circuits, shots=1).result()
        return result.get_counts()

    def _flip(self, bits):
        """
        Flip each bit independently with probability `self.noise`.

        **WHY**: Models additive channel noise / decoherence on the classical output of the channel. Used only by BB84 (E91 injects noise as X-gates directly on the qubits inside the circuit, which is physically more accurate for entangled states).
        """
        mask = self.rng.random(bits.size) < self.noise
        return bits ^ mask

    # ==========================
    # BB84 (prepare-and-measure)
    # ==========================

    def _run_bb84(self, n_bits):
        """
        Run one full BB84 exchange over `n_bits` raw qubits.

        Steps:
        1. Alice picks random bits and random bases (Z/X) for each.
        2. *Optional*: If Eve is active, her probe circuits measure each qubit in her own random basis (phase A), then she resends a freshly prepared state matching her outcome (phase B, built in the receive loop).
        3. Bob measures each received qubit in his random basis.
        4. Sifting: Keep only indices where Alice's and Bob's bases matched.
        5. QBER = Mismatch rate on the sifted key.

        **WHY the two-phase (probe -> resend) structure**: Intercept-resend means Eve first DESTROYS the transmitted state by measuring it, then sends a new state. Both actions must be separate circuits because the resend depends on Eve's measurement result.
        """
        # Alice's secret raw key bits and her random encoding bases.
        bits = self.rng.integers(0, 2, n_bits)
        alice_bases = self.rng.integers(0, 2, n_bits)
        # Bob's independently random measurement bases.
        bob_bases = self.rng.integers(0, 2, n_bits)

        # =========================================
        # Phase A: Eve probes the qubits (optional)
        # =========================================

        probe_circuits = []
        probe_indices = []
        if self.eavesdropper:
            eve_bases = self.rng.integers(0, 2, n_bits)
            for i, (bit, basis, e_basis) in enumerate(
                zip(bits, alice_bases, eve_bases)
            ):
                qc = QuantumCircuit(1, 1)
                # Prepare Alice's state: |0>/|1> (Z) or |+>/|-> (X).
                if bit:
                    qc.x(0)
                if basis == X_BASIS:
                    qc.h(0)
                # Eve measures in HER basis: If she chose X she must rotate the state into the X-basis first (H gate), otherwise she reads in Z.
                if e_basis == X_BASIS:
                    qc.h(0)
                qc.measure(0, 0)
                probe_circuits.append(qc)
                probe_indices.append(i)
            probe_counts = self._batch_measure(probe_circuits)
            # Map pair index -> bit Eve observed (so we can resend it).
            eve_outcomes = {}
            for idx, counts in zip(probe_indices, probe_counts):
                eve_outcomes[idx] = int(next(iter(counts)))
        else:
            # No Eve: Placeholder so the receive loop can skip resend logic.
            eve_bases = np.zeros(n_bits, dtype=int)
            eve_outcomes = {}

        # ==========================================================
        # Phase B: Receive at Bob (with optional Eve resend + noise)
        # ==========================================================

        receive_circuits = []
        receive_indices = []
        for i in range(n_bits):
            if i in eve_outcomes:
                # Eve resends the state she measured (fresh qubit prepared as her outcome, in her basis). This state may be "wrong" for Alice's original encoding -> causes QBER when bases mismatch.
                qc = QuantumCircuit(1, 1)
                qc.initialize([1 - eve_outcomes[i], eve_outcomes[i]])
                if eve_bases[i] == X_BASIS:
                    qc.h(0)
            else:
                # Honest channel: Rebuild Alice's original state for this bit.
                qc = QuantumCircuit(1, 1)
                if bits[i]:
                    qc.x(0)
                if alice_bases[i] == X_BASIS:
                    qc.h(0)
            # Channel Noise: Random bit flip on the qubit (X gate) BEFORE Bob measures, mimicking physical errors on the channel.
            if self.noise and self.rng.random() < self.noise:
                qc.x(0)
            # Bob rotates into his chosen measurement basis, then measures.
            if bob_bases[i] == X_BASIS:
                qc.h(0)
            qc.measure(0, 0)
            receive_circuits.append(qc)
            receive_indices.append(i)
        receive_counts = self._batch_measure(receive_circuits)
        bob_bits = np.empty(n_bits, dtype=int)
        for idx, counts in zip(receive_indices, receive_counts):
            bob_bits[idx] = int(next(iter(counts)))

        # ==============
        # Sifting + QBER
        # ==============

        # Keep only positions where both chose the same basis (publicly agreed via the classical channel in a real system).
        sift = alice_bases == bob_bases
        alice_key = bits[sift]
        bob_key = bob_bits[sift]
        # QBER = Fraction of sifted bits Bob got wrong. >11% => key insecure (Information-theoretic threshold for BB84) and must be aborted.
        qber = (alice_key != bob_key).mean() if alice_key.size else 1.0

        return {
            "protocol": "BB84",
            "alice_key": alice_key,
            "bob_key": bob_key,
            "qber": qber,
            "raw_bits_sent": n_bits,
            "sifted_bits": int(sift.sum()),
            "eve_present": self.eavesdropper,
            "noise": self.noise,
        }

    # ========================
    # E91 (entanglement-based)
    # ========================

    def _run_e91(self, n_pairs):
        """
        Run one full E91 exchange over `n_pairs` shared Bell pairs.

        Steps:
        1. A central source (the Ebit Server in the real system) prepares |phi+> = (|00>+|11>)/sqrt(2) for each pair and sends one qubit to Alice, one to Bob.
        2. Alice and Bob each pick one of 4 measurement angles per pair.
            Key pairs: Both use 0 or both use pi/4 (correlated outcomes -> key bit).
            Test pairs: One uses a KEY angle, the other a TEST angle -> fed into the CHSH computation.
        3. *Optional*: Eve intercepts some pairs, measures both qubits in a random basis, collapsing the entanglement, and resends the product state.
        4. CHSH value S is computed over the 4 test angle combinations. A clean channel gives S ~ 2.83 (Tsirelson bound); S <= 2 means the channel is classical (or Eve has broken the entanglement).

        **WHY measure at angle theta with Ry(-2\\*theta)**: Rotating the state by -2\\*theta before a Z measurement is equivalent to measuring in a basis rotated by theta. With this convention the correlation for |phi+> is exactly E(a,b) = cos(2\\*(a-b)), which yields the textbook CHSH optimum.
        """
        # Indices into ALICE_BASES / BOB_BASES chosen per pair (4 possible).
        alice_angles = self.rng.integers(0, 4, n_pairs)
        bob_angles = self.rng.integers(0, 4, n_pairs)

        # ==================================================
        # Phase A: Eve probes the entangled pairs (optional)
        # ==================================================

        probe_circuits = []
        probe_indices = []
        # Eve attacks half of the pairs (50% intercept rate is the standard choice in QKD security analyses of the intercept-resend attack).
        intercepted = (
            self.rng.random(n_pairs) < 0.5
            if self.eavesdropper
            else np.zeros(n_pairs, bool)
        )
        if intercepted.any():
            probe_bases = self.rng.integers(0, 2, n_pairs)
            for i in np.where(intercepted)[0]:
                qc = QuantumCircuit(2, 2)
                # Prepare the Bell pair |phi+>.
                qc.h(0)
                qc.cx(0, 1)
                # Eve measures both qubits in her random basis: This collapses the entanglement (destroying the correlation Alice/Bob rely on).
                if probe_bases[i] == X_BASIS:
                    qc.h(0)
                    qc.h(1)
                qc.measure(0, 0)
                qc.measure(1, 1)
                probe_circuits.append(qc)
                probe_indices.append(i)
            probe_counts = self._batch_measure(probe_circuits)
            eve_outcomes = {}
            for idx, counts in zip(probe_indices, probe_counts):
                out = next(iter(counts)).replace(" ", "")
                eve_outcomes[idx] = (int(out[0]), int(out[1]))
        else:
            probe_bases = np.zeros(n_pairs, dtype=int)
            eve_outcomes = {}

        # ====================================================
        # Phase B: Alice/Bob measure (with Eve resend + noise)
        # ====================================================

        receive_circuits = []
        receive_indices = []
        for i in range(n_pairs):
            qc = QuantumCircuit(2, 2)
            if i in eve_outcomes:
                # Eve resends the collapsed product state she observed.
                b0, b1 = eve_outcomes[i]
                qc.initialize([1 - b0, b0], 0)
                qc.initialize([1 - b1, b1], 1)
                if probe_bases[i] == X_BASIS:
                    qc.h(0)
                    qc.h(1)
            else:
                # Honest channel: Genuine Bell pair is delivered.
                qc.h(0)
                qc.cx(0, 1)
            # Channel noise as random X flips on each qubit (Physically accurate or entangled states: a flip decoheres the shared state).
            if self.noise:
                for q in range(2):
                    if self.rng.random() < self.noise:
                        qc.x(q)
            # Alice and Bob rotate into their chosen measurement angles.
            qc.ry(-2 * ALICE_BASES[alice_angles[i]], 0)
            qc.ry(-2 * BOB_BASES[bob_angles[i]], 1)
            qc.measure(0, 0)
            qc.measure(1, 1)
            receive_circuits.append(qc)
            receive_indices.append(i)
        receive_counts = self._batch_measure(receive_circuits)
        # measurements[i] = (alice_bit, bob_bit) for pair i.
        measurements = np.zeros((n_pairs, 2), dtype=int)
        for idx, counts in zip(receive_indices, receive_counts):
            out = next(iter(counts)).replace(" ", "")
            measurements[idx] = (int(out[0]), int(out[1]))

        alice_out = measurements[:, 0]
        bob_out = measurements[:, 1]

        # ==============
        # Sifting + QBER
        # ==============

        # Key pairs: Alice and Bob measured in the SAME basis (both 0: angle 0, or both 1: angle pi/4). For |phi+> same-basis outcomes are perfectly correlated, so the measured bit is the shared key bit.
        key_sift = ((alice_angles == 0) & (bob_angles == 0)) | (
            (alice_angles == 1) & (bob_angles == 1)
        )
        alice_key = alice_out[key_sift]
        bob_key = bob_out[key_sift]
        qber = (alice_key != bob_key).mean() if alice_key.size else 1.0

        # Bell test over the remaining pairs certifies the entanglement.
        chsh = self._compute_chsh(measurements, alice_angles, bob_angles)

        return {
            "protocol": "E91",
            "alice_key": alice_key,
            "bob_key": bob_key,
            "qber": qber,
            "chsh": chsh,
            "raw_pairs_sent": n_pairs,
            "sifted_bits": int(key_sift.sum()),
            "eve_present": self.eavesdropper,
            "noise": self.noise,
        }

    def _compute_chsh(self, measurements, alice_angles, bob_angles):
        """
        Estimate the CHSH value S from the measured pairs.

        **WHY**: S is the security certificate for E91 (device-independent). |S| > 2 confirms genuine quantum correlations / entanglement present; S dropping toward/below 2 flags Eve or channel degradation.

        **Math**: For each of the 4 (a,b) angle combinations in CHSH_PAIRS we compute the correlation E(a,b) = P(same) - P(diff) = 2\\*P(same) - 1, then combine with the alternating signs: S = E1 - E2 + E3 + E4.
        """
        S = 0.0
        for sign, (a_angle, b_angle) in zip((1, -1, 1, 1), CHSH_PAIRS):
            # Select only pairs measured with exactly this (a,b) combination.
            mask = (ALICE_BASES[alice_angles] == a_angle) & (
                BOB_BASES[bob_angles] == b_angle
            )
            # Defensive: If by chance no pair fell in this category, skip it (keeps S well-defined for tiny sample sizes).
            if mask.sum() == 0:
                continue
            # E(a,b) = fraction of agreeing outcomes mapped to [-1, +1].
            same = (measurements[mask][:, 0] == measurements[mask][:, 1]).mean()
            S += sign * (2 * same - 1)
        return S

    # ==================
    # Public entry point
    # ==================

    def run(self, n_bits=1024):
        """
        Run the selected protocol once. Dispatches to _run_bb84 or _run_e91 based on the protocol chosen at construction time.
        
        **WHY**: Gives both protocols a single uniform interface so the rest of the system (node.py, split_controller.py) never needs to know which QKD protocol is active.
        """
        if self.protocol == "BB84":
            return self._run_bb84(n_bits)
        return self._run_e91(n_bits)


# =================================
# Module-level convenience function
# =================================

def run_qkd(protocol="BB84", n_bits=1024, noise=0.0, eavesdropper=False, seed=None):
    """
    One-liner wrapper: build an engine and run it.
    """
    engine = QKDEngine(protocol, noise, eavesdropper, seed)
    return engine.run(n_bits)


# ================
# Self-test / Demo
# ================
if __name__ == "__main__":
    for proto in ("BB84", "E91"):
        for eve in (False, True):
            result = run_qkd(proto, n_bits=4096, noise=0.0, eavesdropper=eve, seed=7102133)
            raw = (
                result["raw_bits_sent"]
                if "raw_bits_sent" in result
                else result["raw_pairs_sent"]
            )
            print(
                f"{proto} Eve={eve}: qber={result['qber']:.4f} "
                f"Sifted={result['sifted_bits']}/{raw} "
                f"CHSH={result.get('chsh', float('nan')):.4f}"
            )
