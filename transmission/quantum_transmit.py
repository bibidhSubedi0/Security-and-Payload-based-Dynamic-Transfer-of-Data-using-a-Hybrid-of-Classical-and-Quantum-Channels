"""
Quantum channel transmitter — Phase 5

Encodes a byte string via superdense coding (Phase 1) and returns the decoded
bytes — simulating what Bob receives over the quantum channel.

Bit layout
----------
Each byte is expanded MSB-first into 8 bits, then grouped into consecutive
pairs (bit1, bit0).  encode_and_decode(bit1, bit0) maps 2 classical bits onto
one Bell-state operation and returns the same pair (noiseless simulation).
Received pairs are reassembled MSB-first into bytes.

    byte 0xB4 = 0b10110100
    → bits [1, 0, 1, 1, 0, 1, 0, 0]
    → pairs (1,0), (1,1), (0,1), (0,0)   ← four SDC operations
    → received pairs (1,0), (1,1), (0,1), (0,0)
    → bits [1, 0, 1, 1, 0, 1, 0, 0]
    → byte 0xB4  ✓

Ebit check
----------
One ebit is consumed per 2-bit pair.  Since each byte = 4 pairs:

    ebits_needed(n_bytes) = n_bytes * 4

If available_ebits < ebits_needed, EbitCapacityError is raised immediately.
Phase 4's controller is supposed to have sized the quantum segment to fit within
the available ebit pool; a mismatch here means a logic error upstream, not a
condition to paper over with silent truncation.
"""

from __future__ import annotations

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from qiskit_aer import AerSimulator
from quantum.superdense_coding import encode_and_decode

__all__ = ["EbitCapacityError", "ebits_needed", "encode_bytes_sdc"]

BITS_PER_EBIT = 2   # superdense coding: 2 classical bits per qubit (matches Phase 4)


class EbitCapacityError(Exception):
    """Raised when the quantum segment requires more ebits than are available."""


def ebits_needed(n_bytes: int) -> int:
    """
    Number of ebit-pairs required to transmit n_bytes via superdense coding.
    n_bytes * 8 bits / 2 bits_per_ebit = n_bytes * 4.
    Exact (no ceiling needed) because 8 is evenly divisible by BITS_PER_EBIT.
    """
    return n_bytes * (8 // BITS_PER_EBIT)   # = n_bytes * 4


def encode_bytes_sdc(data: bytes, available_ebits: int) -> bytes:
    """
    Encode data bytes via superdense coding simulation and return decoded bytes.

    In the noiseless simulation this is a round-trip identity (decoded == data).
    In a real system, Alice would apply the encoding unitary on her half of each
    shared Bell pair and send the qubit to Bob; Bob measures jointly.  Here the
    full circuit (Alice's encoding + Bob's decoding) runs locally in AerSimulator.

    Raises EbitCapacityError if available_ebits < ebits_needed(len(data)).
    """
    required = ebits_needed(len(data))
    if required > available_ebits:
        raise EbitCapacityError(
            f"Quantum segment needs {required} ebits "
            f"but only {available_ebits} available. "
            f"Phase 4 split controller should have prevented this."
        )

    if not data:
        return b""

    bits = _bytes_to_bits(data)          # always a multiple of 8 bits
    simulator = AerSimulator()
    received_bits: list[int] = []

    for i in range(0, len(bits), 2):
        result = encode_and_decode(bits[i], bits[i + 1], simulator)
        received_bits.append(result.received[0])
        received_bits.append(result.received[1])

    return _bits_to_bytes(received_bits)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _bytes_to_bits(data: bytes) -> list[int]:
    """Convert bytes to a flat MSB-first bit list."""
    bits: list[int] = []
    for byte in data:
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 1)
    return bits


def _bits_to_bytes(bits: list[int]) -> bytes:
    """Convert MSB-first bit list to bytes (length must be divisible by 8)."""
    assert len(bits) % 8 == 0, f"Bit count {len(bits)} not divisible by 8"
    result = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for shift, bit in enumerate(bits[i : i + 8]):
            byte |= bit << (7 - shift)
        result.append(byte)
    return bytes(result)
