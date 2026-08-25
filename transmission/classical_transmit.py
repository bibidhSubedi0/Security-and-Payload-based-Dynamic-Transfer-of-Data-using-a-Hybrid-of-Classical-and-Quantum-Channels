"""
Classical channel transmitter (Phase 5)

Thin wrappers around classical/aes_channel.py and classical/transport.py
(both Phase 3). No new logic: just the named functions that quantum_transmit's
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
    """
    AES-256-GCM encrypt `data` and send it over `dc`.

    ----------
    Parameters
    ----------
    data : bytes
        Plaintext segment (already fault-injected by the caller if applicable).
    aes_key : bytes
        Session key derived from the reconciled QKD key.
    dc : DataChannel
        Connected classical data channel.

    -----
    Notes
    -----
    GCM is an authenticated mode: any tampering in transit raises InvalidTag
    at the receiver rather than delivering corrupt bytes.
    """
    dc.send(encrypt(data, aes_key))


def recv_classical_segment(dc: DataChannel, aes_key: bytes) -> bytes:
    """
    Receive one framed message from `dc` and AES-256-GCM decrypt it.

    -------
    Returns
    -------
    bytes
        The verified plaintext. Raises InvalidTag on key mismatch or
        tampering; blocks until a frame arrives.
    """
    return decrypt(dc.recv(), aes_key)
