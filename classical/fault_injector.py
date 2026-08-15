"""
Classical channel fault injector — Phase 7b

Config-gated (default OFF).  When enabled, probabilistically corrupts
plaintext bytes BEFORE AES-GCM encryption so corruption survives the
authentication layer and is visible to Phase 6's echo comparison.
Used exclusively for generating synthetic BER / PLR metrics for Phase 8
benchmarking; must NEVER engage during normal Phase 1-6 operation.

Design notes
------------
Plaintext-level injection:
    The injector operates on plaintext (before encrypt() is called), not on
    ciphertext.  Flipping bits in AES-GCM ciphertext causes InvalidTag at the
    receiver — an authentication failure rather than a measurable bit error.
    Injecting on plaintext produces decodable but corrupted bytes on Bob's side,
    which Phase 6's echo comparison detects and reroutes.

Packet-drop simulation:
    A "dropped" packet on TCP would deadlock Bob's recv().  Instead the injector
    records the drop decision and still delivers the data (or zeros of the same
    length).  PLR is therefore a *simulated* metric: "the fraction of packets
    that would have been lost on a real lossy channel at this probability."
    This is clearly documented everywhere it appears — consistent with SNR also
    being a derived proxy rather than a physically measured value.

Config keys (nested under "fault_injection"):
    enabled                : bool   (default False — MUST default off)
    bit_error_probability  : float  (default 0.0)  per-bit flip probability
    packet_loss_probability: float  (default 0.0)  per-packet simulated drop
"""

from __future__ import annotations

import math
import random
import sys
import pathlib
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from metrics.logger import get_logger

__all__ = ["FaultInjector", "InjectionRecord"]

logger = get_logger("fault_injector")


@dataclass
class InjectionRecord:
    """One per process_plaintext() call; included in FaultInjector.history."""
    packet_index:  int
    total_bits:    int
    bit_errors:    int
    simulated_drop: bool   # True if this packet was counted as a drop


class FaultInjector:
    """
    Plaintext-level fault simulator for the classical channel.

    Instantiate from config:
        injector = FaultInjector(config)

    Use before send_classical_segment:
        corrupted = injector.process_plaintext(plaintext)
        send_classical_segment(corrupted, aes_key, dc)

    When disabled (default): process_plaintext() is a no-op identity function.
    """

    def __init__(self, config: dict) -> None:
        fi = config.get("fault_injection", {})
        self.enabled:                 bool  = fi.get("enabled", False)
        self.bit_error_probability:   float = fi.get("bit_error_probability", 0.0)
        self.packet_loss_probability: float = fi.get("packet_loss_probability", 0.0)
        self._rng = random.Random()   # independent RNG so tests can seed it
        self._reset_counters()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_plaintext(self, data: bytes) -> bytes:
        """
        Maybe corrupt plaintext before encryption/send.

        Returns the (possibly corrupted) bytes.
        When disabled, returns data unchanged — zero overhead.
        """
        if not self.enabled:
            return data

        self._total_packets += 1
        idx = self._total_packets
        total_bits = len(data) * 8
        self._total_bits += total_bits

        # --- Packet drop decision (simulated; data still sent) ---
        if self._rng.random() < self.packet_loss_probability:
            self._dropped_packets += 1
            rec = InjectionRecord(
                packet_index=idx,
                total_bits=total_bits,
                bit_errors=0,
                simulated_drop=True,
            )
            self._history.append(rec)
            logger.info("Fault injected: simulated packet drop", extra={
                "packet_index": idx,
                "simulated_only": True,
            })
            # For PLR simulation without protocol deadlock, return zeros
            # so Phase 6 echo detects a mismatch and reroutes.
            return bytes(len(data))

        # --- Bit error injection ---
        buf = bytearray(data)
        errors = 0
        for i in range(len(buf)):
            for bit in range(8):
                if self._rng.random() < self.bit_error_probability:
                    buf[i] ^= (1 << bit)
                    errors += 1
        self._error_bits += errors

        rec = InjectionRecord(
            packet_index=idx,
            total_bits=total_bits,
            bit_errors=errors,
            simulated_drop=False,
        )
        self._history.append(rec)

        if errors:
            logger.info("Fault injected: bit errors", extra={
                "packet_index": idx,
                "bit_errors": errors,
                "total_bits": total_bits,
                "ber_this_packet": round(errors / total_bits, 6) if total_bits else 0,
            })

        return bytes(buf)

    def reset_stats(self) -> None:
        """Clear counters and history (use between test runs for isolation)."""
        self._reset_counters()

    # ------------------------------------------------------------------
    # Derived metrics
    # ------------------------------------------------------------------

    @property
    def total_packets(self) -> int:
        return self._total_packets

    @property
    def total_bits(self) -> int:
        return self._total_bits

    @property
    def error_bits(self) -> int:
        return self._error_bits

    @property
    def dropped_packets(self) -> int:
        return self._dropped_packets

    @property
    def ber(self) -> float:
        """Bit Error Rate = actual flipped bits / total bits processed."""
        if self._total_bits == 0:
            return 0.0
        return self._error_bits / self._total_bits

    @property
    def plr(self) -> float:
        """
        Packet Loss Rate = simulated drops / total packets.

        This is a *simulated* metric: packets are still delivered (as zeros)
        to avoid TCP deadlock; the count reflects what PLR would be on a real
        lossy channel configured with this probability.
        """
        if self._total_packets == 0:
            return 0.0
        return self._dropped_packets / self._total_packets

    @property
    def snr_db(self) -> float | None:
        """
        Simulated SNR in dB derived from BER using a Binary Symmetric Channel
        model: SNR_dB = 10 * log10((1 - BER) / BER).

        Returns None when BER == 0 (perfect channel → infinite SNR).
        Clearly labeled as simulated/derived — not a physically measured value.
        """
        p = self.ber
        if p <= 0.0:
            return None
        if p >= 1.0:
            return -math.inf
        return round(10.0 * math.log10((1.0 - p) / p), 2)

    @property
    def history(self) -> list[InjectionRecord]:
        return list(self._history)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _reset_counters(self) -> None:
        self._total_packets  = 0
        self._dropped_packets = 0
        self._total_bits     = 0
        self._error_bits     = 0
        self._history: list[InjectionRecord] = []
