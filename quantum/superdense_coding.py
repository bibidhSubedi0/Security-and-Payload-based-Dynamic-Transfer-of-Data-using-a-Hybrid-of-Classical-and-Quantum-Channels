"""Superdense coding: encode 2 classical bits into 1 qubit operation on a Bell pair."""

from dataclasses import dataclass

from qiskit import QuantumCircuit
from qiskit_aer import AerSimulator


# Encoding map: (bit1, bit0) → unitary Alice applies to her qubit
# |Φ+> = (|00> + |11>)/√2 is the shared Bell state
#   00 → I         → |Φ+>
#   01 → X         → |Ψ+>
#   10 → Z         → |Φ->
#   11 → ZX = iY   → |Ψ->
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
    sent: tuple[int, int]
    received: tuple[int, int]
    success: bool


def encode_and_decode(bit1: int, bit0: int, simulator: AerSimulator | None = None) -> SDCResult:
    """
    Full SDC round-trip for a single 2-bit message.

    Circuit layout:
      q0 = Alice's qubit (the one she operates on)
      q1 = Bob's qubit  (stays with Bob)

    1. Entangle: H on q0, CNOT q0→q1  →  |Φ+>
    2. Alice encodes (bit1, bit0) by applying a local gate on q0
    3. Bob decodes: CNOT q0→q1, H on q0, measure both
    """
    if simulator is None:
        simulator = AerSimulator()

    qc = QuantumCircuit(2, 2)

    # Step 1: Create |Φ+>
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
    """Run all four 2-bit messages and return results."""
    simulator = AerSimulator()
    results = []
    for bit1 in range(2):
        for bit0 in range(2):
            results.append(encode_and_decode(bit1, bit0, simulator))
    return results
