r"""
Superdense Coding: 2 Classical Bits Per Qubit
==============================================

Implements the superdense coding protocol (Bennett & Wiesner, 1992) where Alice
encodes 2 classical bits into 1 qubit operation on a pre-shared Bell pair.

-----------------
Protocol Overview
-----------------
1. Setup: A trusted source distributes Bell state |$\phi$+> = (|00> + |11>)/$\sqrt(2)$.
   Alice holds qubit 0, Bob holds qubit 1.
2. Alice receives 2-bit message (bit1, bit0) and applies a unitary to her qubit:
     00 -> I   (identity)     -> |$\phi$+>
     01 -> X   (bit flip)     -> |$\psi$+>
     10 -> Z   (phase flip)   -> |$\phi$->
     11 -> ZX = iY            -> |$\psi$->
3. Alice sends her qubit to Bob (quantum channel).
4. Bob performs Bell basis measurement:
     CNOT q0->q1, H on q0, measure both qubits in Z basis.
   Measurement outcomes map back to the 2-bit message:
     00 -> (0,0), 10 -> (0,1), 01 -> (1,0), 11 -> (1,1).

------------
Key Property
------------
Transmits 2 classical bits by sending 1 qubit, CONDITIONED on pre-shared
entanglement. Does not violate Holevo bound because entanglement was
established beforehand (requires prior quantum channel use).

----------------------------
Encoding Map (ENCODING dict)
----------------------------
(bit1, bit0) -> unitary Alice applies to her qubit (q0)
  (0, 0) -> "I"   (identity)
  (0, 1) -> "X"   (Pauli-X)
  (1, 0) -> "Z"   (Pauli-Z)
  (1, 1) -> "ZX"  (Z then X, equivalent to iY up to global phase)

----------------------------
Decoding Map (DECODING dict)
----------------------------
Bob's measurement outcomes (q1, q0) -> recovered (bit1, bit0)
  "00" -> (0, 0)
  "10" -> (0, 1)
  "01" -> (1, 0)
  "11" -> (1, 1)
Note: Qiskit little-endian convention: rightmost bit = q0 measurement.

---------------
Data Structures
---------------
SDCResult (dataclass):
    sent: tuple[int, int]     # (bit1, bit0) Alice intended to send
    received: tuple[int, int] # (bit1, bit0) Bob decoded
    success: bool             # True if received == sent

---------
Functions
---------
encode_and_decode(bit1, bit0, simulator=None) -> SDCResult
    Full SDC round-trip for a single 2-bit message.
    Creates circuit, runs simulation, returns result.
    If simulator not provided, creates new AerSimulator().

run_superdense_coding_all() -> list[SDCResult]
    Runs all four 2-bit messages (00, 01, 10, 11) sequentially.
    Returns list of 4 SDCResult objects.
    Used for protocol verification and testing.

--------------
Circuit Layout
--------------
Qubit indices:
  q0 = Alice's qubit (the one she operates on and sends to Bob)
  q1 = Bob's qubit  (stays with Bob throughout)

Step 1: Entangle
  H(0), CX(0,1) -> |Phi+> = (|00> + |11>)/$\sqrt(2)$

Step 2: Alice encodes on q0
  00: I (nothing)
  01: X(0)
  10: Z(0)
  11: Z(0) then X(0)

Step 3: Bob decodes
  CX(0,1), H(0), measure q0->c0, q1->c1

---------------------------------------
Measurement Mapping (Qiskit Convention)
---------------------------------------
Qiskit returns bitstring "c1 c0" with spaces, rightmost = c0 = q0 measure.
After Bob's decoding circuit:
  q0 measurement = bit0 (LSB)
  q1 measurement = bit1 (MSB)
Raw string "b1 b0" -> bits = b1 + b0 -> DECODING[bits] = (bit1, bit0).

--------------
Security Notes
--------------
- NOT a QKD protocol. Does not generate secret key.
- Presupposes pre-shared entanglement (Bell pair).
- If Eve intercepts Alice's qubit in transit, she learns nothing without
  Bob's half of the Bell pair (monogamy of entanglement).
- In this simulation: trusted devices, no loss/noise modeling.
- Practical use: quantum network links where entanglement is pre-distributed
  (e.g., quantum repeaters, satellite links).

-----------
Integration
-----------
Currently an auxiliary demonstration module. Not used in the primary
session key generation path (which uses BB84/E91). Could be integrated
for quantum-enhanced classical communication on established entangled links.
"""

from dataclasses import dataclass

from qiskit import QuantumCircuit
from qiskit_aer import AerSimulator


# Encoding map: (bit1, bit0) -> unitary Alice applies to her qubit
# |Phi+> = (|00> + |11>)/$\sqrt(2)$ is the shared Bell state
#   00 -> I         -> |Phi+>
#   01 -> X         -> |Psi+>
#   10 -> Z         -> |Phi->
#   11 -> ZX = iY   -> |Psi->
ENCODING = {
    (0, 0): "I",
    (0, 1): "X",
    (1, 0): "Z",
    (1, 1): "ZX",
}

DECODING = {
    "00": (0, 0),
    "10": (0, 1),
    "01": (1, 0),
    "11": (1, 1),
}


@dataclass
class SDCResult:
    r"""
    Result of a single superdense coding round-trip.

    ----------
    Attributes
    ----------
    sent : tuple[int, int]
        The 2-bit message Alice encoded: (bit1, bit0).
    received : tuple[int, int]
        The 2-bit message Bob decoded: (bit1, bit0).
    success : bool
        True if received == sent. In noiseless simulation, always True.
    """
    sent: tuple[int, int]
    received: tuple[int, int]
    success: bool


def encode_and_decode(bit1: int, bit0: int, simulator: AerSimulator | None = None) -> SDCResult:
    r"""
    Full SDC round-trip for a single 2-bit message.

    ----------
    Parameters
    ----------
    bit1 : int
        Most significant bit of the 2-bit message (0 or 1).
    bit0 : int
        Least significant bit of the 2-bit message (0 or 1).
    simulator : AerSimulator or None, default None
        Qiskit Aer simulator instance. If None, creates a new one.

    -------
    Returns
    -------
    SDCResult
        Contains sent bits, received bits, and success flag.

    --------------
    Circuit Layout
    --------------
    q0 = Alice's qubit (the one she operates on)
    q1 = Bob's qubit  (stays with Bob)

    Step 1: Entangle: H on q0, CNOT q0->q1  ->  |Phi+>
    Step 2: Alice encodes (bit1, bit0) by applying a local gate on q0
    Step 3: Bob decodes: CNOT q0->q1, H on q0, measure both
    """
    if simulator is None:
        simulator = AerSimulator()

    qc = QuantumCircuit(2, 2)

    # Step 1: Create |Phi+>
    qc.h(0)
    qc.cx(0, 1)

    # Step 2: Alice's encoding on qubit 0
    op = ENCODING[(bit1, bit0)]
    if "X" in op:
        qc.x(0)
    if "Z" in op:
        qc.z(0)

    # Step 3: Bob's decoding
    qc.cx(0, 1)
    qc.h(0)
    qc.measure(0, 0)
    qc.measure(1, 1)

    if simulator is None:
        simulator = AerSimulator()
    job = simulator.run(qc, shots=1)
    counts = job.result().get_counts()
    raw = list(counts.keys())[0].replace(" ", "")
    # raw is "c1c0" in Qiskit's little-endian convention: rightmost = c0 = q0 measure
    q0_bit = int(raw[-1])
    q1_bit = int(raw[-2])
    received = DECODING[f"{q1_bit}{q0_bit}"]

    return SDCResult(
        sent=(bit1, bit0),
        received=received,
        success=received == (bit1, bit0),
    )


def run_superdense_coding_all() -> list[SDCResult]:
    r"""
    Run all four 2-bit messages and return results.

    -------
    Returns
    -------
    list[SDCResult]
        Four results for messages 00, 01, 10, 11 in that order.
        All should have success=True in noiseless simulation.

    Used by tests/test_superdense_coding.py for protocol verification.
    """
    simulator = AerSimulator()
    results = []
    for bit1 in range(2):
        for bit0 in range(2):
            results.append(encode_and_decode(bit1, bit0, simulator))
    return results