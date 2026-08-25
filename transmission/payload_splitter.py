"""
Payload splitter (Phase 5)

Splitting scheme
----------------
Given a SplitDecision from Phase 4 and a payload byte-string:

    quantum_len      = int(quantum_fraction * len(payload))   # floor
    quantum_segment  = payload[:quantum_len]                  # HEAD of payload
    classical_segment = payload[quantum_len:]                 # TAIL of payload

The quantum-protected segment is placed at the HEAD because the beginning of a
message typically carries the most critical information (sequence numbers, headers,
identifiers), and the quantum channel provides the stronger confidentiality guarantee.

Reassembly order (Phase 6 builds on this):
    original = quantum_segment + classical_segment

Using int() (floor) ensures: quantum_len + (len(payload) - quantum_len) == len(payload)
exactly, with no rounding error. The metadata dict carries quantum_len and total_len
so Node B never has to recompute or guess the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from split_controller.controller import SplitDecision

__all__ = [
    "SplitPayload",
    "split_payload",
    "make_metadata",
    "reassemble_from_segments",
    "ReassemblyError",
]


class ReassemblyError(Exception):
    """Raised when the received segments don't match the declared metadata."""


@dataclass
class SplitPayload:
    """
    Result of split_payload(): both segments plus the bookkeeping Bob needs.

    ----------
    Attributes
    ----------
    quantum_segment : bytes
        HEAD of the payload, routed via superdense coding.
    classical_segment : bytes
        TAIL of the payload, routed AES-256-GCM encrypted over TCP.
    quantum_len : int
        len(quantum_segment); duplicated as an int so receivers never
        recompute the boundary from floats.
    classical_len : int
        len(classical_segment).
    total_len : int
        quantum_len + classical_len == original payload length.
    quantum_fraction_used : float
        The decision's fraction actually applied (post-floor); recorded for
        metrics/logging rather than reassembly.
    """

    quantum_segment:   bytes
    classical_segment: bytes
    quantum_len:       int    # == len(quantum_segment)
    classical_len:     int    # == len(classical_segment)
    total_len:         int    # == quantum_len + classical_len
    quantum_fraction_used: float


def split_payload(payload: bytes, decision: SplitDecision) -> SplitPayload:
    """
    Partition payload according to decision.

    quantum_len is floored so the byte boundary is exact and reproducible.
    """
    quantum_len      = int(decision.quantum_fraction * len(payload))
    quantum_segment  = payload[:quantum_len]
    classical_segment = payload[quantum_len:]
    return SplitPayload(
        quantum_segment=quantum_segment,
        classical_segment=classical_segment,
        quantum_len=quantum_len,
        classical_len=len(classical_segment),
        total_len=len(payload),
        quantum_fraction_used=decision.quantum_fraction,
    )


def make_metadata(split: SplitPayload) -> dict:
    """
    Serialisable metadata sent to Bob before transmission starts.
    Contains everything Bob needs for reassembly without re-computing.
    """
    return {
        "quantum_len":  split.quantum_len,
        "classical_len": split.classical_len,
        "total_len":    split.total_len,
        "quantum_fraction_used": split.quantum_fraction_used,
    }


def reassemble_from_segments(
    quantum_bytes: bytes,
    classical_bytes: bytes,
    metadata: dict,
) -> bytes:
    """
    Reconstruct original payload from the two received segments.

    Validates lengths against declared metadata and raises ReassemblyError
    if they don't match; this catches truncation or mix-ups before Phase 6's
    echo comparison sees corrupted data.
    """
    expected_q = metadata["quantum_len"]
    expected_c = metadata["classical_len"]
    expected_total = metadata["total_len"]

    if len(quantum_bytes) != expected_q:
        raise ReassemblyError(
            f"Quantum segment length mismatch: "
            f"expected {expected_q} bytes, got {len(quantum_bytes)}"
        )
    if len(classical_bytes) != expected_c:
        raise ReassemblyError(
            f"Classical segment length mismatch: "
            f"expected {expected_c} bytes, got {len(classical_bytes)}"
        )
    reconstructed = quantum_bytes + classical_bytes
    if len(reconstructed) != expected_total:
        raise ReassemblyError(
            f"Reassembled length {len(reconstructed)} != declared total {expected_total}"
        )
    return reconstructed
