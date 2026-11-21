"""
AES-256-GCM encryption/decryption for the classical channel in the hybrid QKD-classical system.
===============================================================================================

--------------
MODULE PURPOSE
--------------
This module provides authenticated encryption for the classical communication channel
in a hybrid quantum-classical data transfer system. It serves as the classical
counterpart to the quantum key distribution (QKD) channel, using keys derived from
QKD to encrypt classical payloads.

---------------------------
ARCHITECTURAL RELATIONSHIP:
---------------------------
- Consumes: Raw QKD key bits from quantum channel (via derive_key)
- Produces: AES-256 keys for symmetric encryption
- Used by: Classical channel transmitter/receiver for payload encryption
- Integrates with: QKD key exchange, classical payload transport

---------------------------
KEY DERIVATION (derive_key)
---------------------------
QKD produces a variable-length bit string (typically ~50-100 bits for 200 qubits
with ~50% basis agreement and half sacrificed for QBER estimation). This module
uses HKDF-SHA256 to deterministically stretch/compress any input length to exactly
32 bytes (256 bits) suitable for AES-256.

---------
WHY HKDF:
---------
- Cryptographic key derivation function designed for this exact purpose
- SHA256 provides 256-bit security strength matching AES-256
- Domain separation via info parameter prevents cross-protocol key reuse
- Salt=None is acceptable here because QKD bits have high min-entropy

------------------------------------------
AUTHENTICATED ENCRYPTION (encrypt/decrypt)
------------------------------------------
AES-GCM (Galois/Counter Mode) is used instead of CBC because:
1. AUTHENTICATION: Any ciphertext modification raises InvalidTag on decrypt
   (vs CBC which silently returns corrupted plaintext)
2. PARALLELIZATION: CTR mode allows parallel encryption/decryption
3. NONCE-BASED: 96-bit random nonce per message (no IV management complexity)
4. STANDARDIZED: NIST SP 800-38D, widely implemented and analyzed

WIRE FORMAT (encrypt output / decrypt input):
    [ nonce: 12 bytes ][ ciphertext + auth_tag: len(plaintext) + 16 bytes ]
    ^^^^^^^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    Prepended to       AES-GCM produces ciphertext || tag internally
    ciphertext

--------------------
SECURITY PROPERTIES:
--------------------
- IND-CCA2: Indistinguishability under adaptive chosen-ciphertext attack
- INT-CTXT: Integrity of ciphertext (auth tag prevents forgery)
- Key separation: Each message gets unique nonce; key reuse safe with unique nonces
- Nonce misuse resistance: GCM fails catastrophically on nonce reuse (keys must be rotated)
"""

import os

from cryptography.exceptions import InvalidTag  # re-exported for caller convenience
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = ["derive_key", "encrypt", "decrypt", "InvalidTag"]

# -----------------------------------------------------------------------------
# CRYPTOGRAPHIC CONSTANTS
# These values are fixed by the AES-GCM and HKDF-SHA256 specifications.
# DO NOT CHANGE without updating security analysis.
# -----------------------------------------------------------------------------

# AES-GCM nonce size: 96 bits (12 bytes) — NIST SP 800-38D recommended length
# Using 96-bit nonces allows direct use of random nonces without counter management.
# Probability of collision after 2^32 messages is negligible (~2^-32).
_NONCE_SIZE = 12

# AES-256 key size: 256 bits (32 bytes) — matches QKD security target
# HKDF-SHA256 output length fixed to this value.
_KEY_SIZE = 32

# HKDF domain separation label: prevents key reuse across different protocols
# Format: "project-protocol-algorithm" for unique identification
# This ensures keys derived here cannot be confused with keys from other contexts
_KDF_INFO = b"hbd-qkd-classical-aes256"


def derive_key(qkd_bits: list[int]) -> bytes:
    """
    Derive a 32-byte AES-256 key from raw QKD key bits using HKDF-SHA256.

    --------
    PURPOSE:
    --------
    Converts variable-length QKD bit string (output of quantum key exchange)
    into fixed-length symmetric key for AES-256-GCM encryption.

    ------
    INPUT:
    ------
    - qkd_bits: List of integers (0 or 1) representing raw key from QKD
                Typical length: 50-100 bits (after basis sifting + QBER sacrifice)
                Minimum: 1 bit (HKDF handles stretching)

    --------
    PROCESS:
    --------
    1. Pack bits big-endian into bytes (MSB first, zero-padded to byte boundary)
       Example: [1,0,1] → 0b10100000 → bytes([0xA0])
    2. Feed as Input Key Material (IKM) to HKDF-SHA256
    3. HKDF expands/extracts to exactly 32 bytes using domain-separated info

    ----------------
    WHY THIS DESIGN:
    ----------------
    - Bit packing preserves entropy: each QKD bit contributes to key entropy
    - Big-endian is standard for network byte order (consistent with QKD protocols)
    - HKDF-SHA256: NIST SP 800-56C compliant, proven secure in ROM
    - salt=None: QKD bits have high min-entropy (~1 bit per raw bit before EC)
      Salt adds security when IKM has low entropy (not our case)
    - info=_KDF_INFO: Domain separation prevents cross-protocol key confusion

    -------
    OUTPUT:
    -------
    - bytes: Exactly 32 bytes (256 bits) suitable for AES-256-GCM

    -------
    RAISES:
    -------
    - ValueError: If qkd_bits is empty (no entropy source)

    --------------
    SECURITY NOTE:
    --------------
    The derived key inherits security from QKD: if QBER < threshold, Eve's
    information is bounded by privacy amplification. HKDF acts as randomness
    extractor + key expansion.
    """
    if not qkd_bits:
        raise ValueError("Cannot derive key from empty QKD bit list")

    # Pack bit list → bytes (big-endian, zero-padded to byte boundary)
    # Each bit shifts left, LSB gets the new bit → MSB-first packing
    n = len(qkd_bits)
    value = 0
    for bit in qkd_bits:
        value = (value << 1) | int(bit)
    byte_count = (n + 7) // 8  # ceiling division: bits → bytes
    ikm = value.to_bytes(byte_count, "big")

    # HKDF-SHA256: Extract-then-Expand paradigm
    # Extract: HMAC-SHA256(salt, IKM) → PRK (pseudorandom key)
    # Expand:  HMAC-SHA256(PRK, info || counter) → OKM (output key material)
    hkdf = HKDF(
        algorithm=hashes.SHA256(),     # Hash function for HMAC
        length=_KEY_SIZE,              # Output length: 32 bytes = 256 bits
        salt=None,                     # No salt: QKD IKM has high min-entropy
        info=_KDF_INFO,                # Domain separation: "hbd-qkd-classical-aes256"
    )
    return hkdf.derive(ikm)


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    """
    AES-256-GCM encrypt plaintext with a 32-byte key.

    --------
    PURPOSE:
    --------
    Provides authenticated encryption for classical channel payloads using
    keys derived from QKD. Each call generates a unique random nonce.

    ------
    INPUT:
    ------
    - plaintext: bytes to encrypt (any length, including empty)
    - key: 32-byte AES-256 key (from derive_key(qkd_bits))

    --------
    PROCESS:
    --------
    1. Validate key length (must be exactly 32 bytes)
    2. Generate cryptographically random 96-bit nonce (12 bytes)
    3. Initialize AES-GCM with key
    4. Encrypt: CTR mode encryption + GMAC authentication tag
    5. Return: nonce || ciphertext || tag (concatenated)

    ------------
    WHY AES-GCM:
    ------------
    - Authenticated encryption: detects ANY ciphertext modification
    - Parallelizable: CTR mode allows multi-block parallelism
    - Single pass: encryption + authentication in one operation
    - Standard: NIST SP 800-38D, TLS 1.2/1.3 mandatory cipher

    -----------------
    NONCE GENERATION:
    -----------------
    - os.urandom(12) → 96 random bits from OS CSPRNG
    - 96-bit space: collision probability ~2^-32 after 2^32 messages
    - Random nonces acceptable for GCM (vs counter-based for high-volume)
    - Each encryption call gets fresh nonce → key reuse safe

    ---------------------
    ASSOCIATED DATA (AD):
    ---------------------
    - None passed here (ad=None)
    - Could include: message headers, sequence numbers, timestamps
    - Adding AD would bind ciphertext to context (prevent replay/reorder)
    - Future enhancement: pass packet header as AD

    OUTPUT FORMAT (wire format):
        [ nonce: 12 bytes ][ ciphertext: len(plaintext) bytes ][ tag: 16 bytes ]
        ^^^^^^^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ ^^^^^^^^^^^^^^^
        Prepended           AES-CTR output                    GMAC auth tag
        for decrypt         (same length as plaintext)        (fixed 128-bit)

    --------
    RETURNS:
    --------
    - bytes: nonce + ciphertext + tag (total len = 12 + len(plaintext) + 16)

    -------
    RAISES:
    -------
    - ValueError: If key length != 32 bytes

    --------------------
    SECURITY PROPERTIES:
    --------------------
    - IND-CPA: Ciphertext reveals no plaintext info (under random nonce)
    - INT-CTXT: Any modification detected via auth tag verification
    - Nonce reuse catastrophic: same (key, nonce) twice → keystream reuse → break
      Mitigation: key rotation after ~2^32 messages or use deterministic nonce
    """
    if len(key) != _KEY_SIZE:
        raise ValueError(f"Key must be {_KEY_SIZE} bytes, got {len(key)}")
    nonce = os.urandom(_NONCE_SIZE)
    aesgcm = AESGCM(key)
    ciphertext_tag = aesgcm.encrypt(nonce, plaintext, None)
    return nonce + ciphertext_tag


def decrypt(data: bytes, key: bytes) -> bytes:
    """
    AES-256-GCM decrypt data encrypted by encrypt().

    --------
    PURPOSE:
    --------
    Verifies authenticity and decrypts classical channel payloads. Any
    tampering, wrong key, or corruption raises InvalidTag (not silent failure).

    ------
    INPUT:
    ------
    - data: bytes in wire format from encrypt()
            Must be: nonce (12) + ciphertext (N) + tag (16) = at least 28 bytes
    - key: 32-byte AES-256 key (same as used for encrypt, from derive_key)

    --------
    PROCESS:
    --------
    1. Validate key length (32 bytes)
    2. Validate minimum data length (nonce + tag = 28 bytes minimum)
    3. Split: nonce = data[:12], ciphertext_tag = data[12:]
    4. Initialize AES-GCM with key
    5. Decrypt: verify GMAC tag, then CTR-mode decrypt
    6. Return plaintext on success

    ------------------
    VALIDATION CHECKS:
    ------------------
    - Key length: Prevents misuse with wrong-size keys
    - Data length: Catches truncation attacks early (before crypto ops)
    - Auth tag verification: Cryptographic integrity check (constant-time)

    ---------------
    ERROR HANDLING:
    ---------------
    - ValueError: Key wrong size or data too short (malformed input)
    - InvalidTag: Auth tag mismatch (tampered ciphertext, wrong key, corruption)
      Raised by cryptography library, re-exported for caller handling

    -------------------------------
    WHY InvalidTag NOT CAUGHT HERE:
    -------------------------------
    - Caller must decide: retry, alert, drop connection, log attack
    - Silent failure would hide active attacks (padding oracle style)
    - Exception propagates to protocol layer for appropriate response

    ------------------------
    SIDE-CHANNEL RESISTANCE:
    ------------------------
    - cryptography library uses constant-time tag comparison
    - No timing leak on tag verification failure
    - Early length check is public-input dependent (safe)

    --------
    RETURNS:
    --------
    - bytes: Original plaintext (same length as encrypt input)

    -------
    RAISES:
    -------
    - ValueError: Key != 32 bytes OR data < 28 bytes
    - InvalidTag: Authentication failure (ciphertext tampered/wrong key/truncated)

    --------------
    SECURITY NOTE:
    --------------
    InvalidTag means ACTIVE ATTACK or KEY MISMATCH. Do not retry with same key
    on same ciphertext. Protocol should: terminate session, rotate keys, alert.
    """
    if len(key) != _KEY_SIZE:
        raise ValueError(f"Key must be {_KEY_SIZE} bytes, got {len(key)}")
    if len(data) < _NONCE_SIZE + 16:  # nonce + minimum 16-byte tag
        raise ValueError("Data too short to be a valid AES-GCM payload")
    nonce = data[:_NONCE_SIZE]
    ciphertext_tag = data[_NONCE_SIZE:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext_tag, None)
