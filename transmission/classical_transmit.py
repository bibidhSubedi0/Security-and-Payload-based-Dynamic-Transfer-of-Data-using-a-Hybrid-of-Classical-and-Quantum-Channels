"""
Classical channel transmitter — Phase 5

Thin wrappers around classical/aes_channel.py and classical/transport.py
(both Phase 3). No new logic — just the named functions that quantum_transmit's
symmetric partner uses so the concurrency wiring in Node is symmetric.
"""

from __future__ import annotations

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from classical.aes_channel import encrypt, decrypt
from classical.transport import DataChannel

__all__ = ["send_classical_segment", "recv_classical_segment"]


def send_classical_segment(data: bytes, aes_key: bytes, dc: DataChannel) -> None:
    """AES-256-GCM encrypt data and send over dc."""
    dc.send(encrypt(data, aes_key))


def recv_classical_segment(dc: DataChannel, aes_key: bytes) -> bytes:
    """Receive from dc and AES-256-GCM decrypt."""
    return decrypt(dc.recv(), aes_key)
