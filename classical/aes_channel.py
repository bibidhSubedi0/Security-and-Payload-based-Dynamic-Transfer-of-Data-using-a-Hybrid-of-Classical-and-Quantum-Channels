"""
AES-256-GCM encryption/decryption for the classical channel.

Key derivation
--------------
QKD produces a list of bits. Their raw length varies (~50 bits for 200 qubits with
~50% basis agreement, half sacrificed to QBER). HKDF-SHA256 is used to stretch or
compress this to exactly 32 bytes (256 bits) regardless of QKD key length.

Authenticated encryption
------------------------
AES-GCM is used rather than CBC so that any ciphertext tampering causes decrypt() to
raise InvalidTag rather than silently returning garbage. The 96-bit nonce is randomly
generated per call and prepended to the wire payload; the 128-bit GCM auth tag is
appended automatically by the cryptography library.

Wire format (returned by encrypt, consumed by decrypt):
    nonce (12 bytes) | ciphertext+tag (len(plaintext) + 16 bytes)
"""

import os

from cryptography.exceptions import InvalidTag  # re-exported for caller convenience
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = ["derive_key", "encrypt", "decrypt", "InvalidTag"]

_NONCE_SIZE = 12                         # 96 bits — GCM recommended nonce length
_KEY_SIZE   = 32                         # 256 bits
_KDF_INFO   = b"hbd-qkd-classical-aes256"


def derive_key(qkd_bits: list[int]) -> bytes:
    """
    Derive a 32-byte AES-256 key from the raw QKD bit list using HKDF-SHA256.

    The bit list is packed big-endian into bytes (zero-padded to the nearest byte),
    then passed as HKDF input key material. Works for any list length ≥ 1.
    """
    if not qkd_bits:
        raise ValueError("Cannot derive key from empty QKD bit list")

    # Pack bit list → bytes (big-endian, zero-padded to byte boundary)
    n = len(qkd_bits)
    value = 0
    for bit in qkd_bits:
        value = (value << 1) | int(bit)
    byte_count = (n + 7) // 8
    ikm = value.to_bytes(byte_count, "big")

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_SIZE,
        salt=None,       # no shared salt yet; Phase 6 can add session nonce here
        info=_KDF_INFO,
    )
    return hkdf.derive(ikm)


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    """
    AES-256-GCM encrypt plaintext with key.

    Returns nonce + ciphertext_with_tag. Each call uses a fresh random nonce.
    key must be 32 bytes (use derive_key() to produce it from QKD bits).
    """
    if len(key) != _KEY_SIZE:
        raise ValueError(f"Key must be {_KEY_SIZE} bytes, got {len(key)}")
    nonce = os.urandom(_NONCE_SIZE)
    aesgcm = AESGCM(key)
    ciphertext_tag = aesgcm.encrypt(nonce, plaintext, None)
    return nonce + ciphertext_tag


def decrypt(data: bytes, key: bytes) -> bytes:
    """
    AES-256-GCM decrypt.

    data must be in the format returned by encrypt(): nonce + ciphertext_with_tag.
    Raises cryptography.exceptions.InvalidTag if the auth tag does not match
    (tampered ciphertext, wrong key, truncated data).
    """
    if len(key) != _KEY_SIZE:
        raise ValueError(f"Key must be {_KEY_SIZE} bytes, got {len(key)}")
    if len(data) < _NONCE_SIZE + 16:  # nonce + minimum 16-byte tag
        raise ValueError("Data too short to be a valid AES-GCM payload")
    nonce = data[:_NONCE_SIZE]
    ciphertext_tag = data[_NONCE_SIZE:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext_tag, None)
