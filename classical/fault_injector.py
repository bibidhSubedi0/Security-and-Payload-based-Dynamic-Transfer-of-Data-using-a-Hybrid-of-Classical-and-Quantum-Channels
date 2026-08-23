r"""
Classical Channel Fault Injector - Phase 7b
===========================================

Simulates an imperfect classical channel by probabilistically corrupting
plaintext bytes BEFORE AES-GCM encryption. Used exclusively to generate
synthetic BER / PLR / SNR data for Phase 8 benchmarking; must NEVER engage
during normal Phase 1-6 operation (config-gated, default OFF).

-----------------
Role in Pipeline
-----------------
Positioned between payload assembly (Phase 5 transmit) and encryption:

    Alice: payload_splitter -> [injector.process_plaintext()] -> aes_channel.encrypt
           -> transport.send_classical_segment -> TCP
    Bob:   recv -> decrypt -> reassemble
    Phase 6 (echo_validation.py): Bob echoes reassembled payload; Alice compares
           byte-for-byte and patches any corrupted ranges over the classical link.

Because injection happens on plaintext, the corruption survives decryption and
is visible to Phase 6's echo comparison, which is the component that reacts
to it (reroute/patch). Injecting anywhere after encrypt() would instead surface
as an InvalidTag authentication failure at the receiver, which is a different,
non-measurable event (see Design Notes).

-------------
Design Notes
-------------
Plaintext-level injection:
    AES-GCM is an authenticated cipher: flipping one ciphertext or tag bit
    causes the entire decryption to fail with InvalidTag. That would make bit
    errors unobservable as *bit* errors. Injecting pre-encryption produces
    decodable-but-corrupted plaintext on Bob's side, which Phase 6 detects and
    repairs via its patch/retransmit path.

Packet-drop simulation:
    A genuinely withheld packet would deadlock Bob's blocking recv() on TCP
    (TCP guarantees delivery). Instead the injector *records* the drop decision
    and still delivers data: zeros of the SAME length as the original.
    Same-length is required so that payload_splitter offsets and Phase 6's
    byte-range mismatch localization remain valid. PLR is therefore a
    *simulated* metric: "what PLR would be on a real lossy channel at this
    probability". Same caveat applies to snr_db (derived, not physical).

Why a single shared instance:
    Counters accumulate across all packets of a session, giving session-level
    BER/PLR rather than per-packet values. scripts/run_benchmark.py creates one
    injector per scenario run and hands the same object to metrics.collector,
    which snapshots the final counters into the TransmissionRecord.

----------------------------
Configuration (config dict)
----------------------------
Reads the nested key "fault_injection"; missing key => disabled defaults.
Keys, types, defaults, and UNITS:

    enabled                 : bool   (default False, MUST default off;
                                       safety gate for the whole module)
    bit_error_probability   : float  (default 0.0)
                                       UNIT: probability per bit, dimensionless,
                                       range [0, 1]. Expected value of flips per
                                       packet = 8 * len(data) * p.
    packet_loss_probability : float  (default 0.0)
                                       UNIT: probability per packet call,
                                       dimensionless, range [0, 1].

------------
Components
------------
InjectionRecord (dataclass):
    Per-packet audit entry appended to FaultInjector.history on every enabled
    process_plaintext() call. Fields carry the raw counts from which the
    aggregate properties (ber, plr) are derived.

FaultInjector (class):
    Stateless-per-call corrupter + stateful counter store. Public surface:
      - process_plaintext(data) -> bytes   (call immediately before encryption)
      - reset_stats()                      (isolation between runs)
      - Properties: total_packets, total_bits, error_bits, dropped_packets,
        ber, plr, snr_db, history  (consumed by metrics/collector.py)

-----------
Functions
-----------
process_plaintext(data) -> bytes
    Drop decision first (a "dropped" packet receives no bit errors); otherwise
    flips random bits. Disabled => identity function, zero overhead.

reset_stats()
    Zeroes all counters and clears history.

ber / plr / snr_db (properties)
    Derived metrics; formulas and edge cases documented on each property.

-----------
Integration
-----------
  - scripts/run_benchmark.py: builds FaultInjector per fault scenario, wraps
    every classical segment with process_plaintext() before send.
  - metrics/collector.py: reads .enabled/.ber/.plr/.snr_db/.error_bits/
    .dropped_packets into TransmissionRecord fields fault_injection_enabled,
    ber_simulated, plr_simulated, snr_db_simulated.
  - transmission/echo_validation.py: downstream consumer of the corruption
    (detects mismatch, classifies source, patches).
  - tests/test_fault_injector.py: off-by-default, BER/PLR statistics, SNR
    monotonicity; seeds injector._rng directly for determinism.

--------------
Security Notes
--------------
- Default OFF is a hard requirement: this is a test instrument, not a feature.
- Injection is plaintext-mutation only; it never touches keys, nonces, or the
  AES-GCM layer itself.
"""

from __future__ import annotations

import math
import random
import sys
import pathlib
from dataclasses import dataclass, field

# Make the project root importable when this file is executed outside the
# package context (e.g. `python classical/fault_injector.py`) so that
# `from metrics.logger import ...` resolves without installation.
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from metrics.logger import get_logger

# Public API of this module: everything else is implementation detail.
__all__ = ["FaultInjector", "InjectionRecord"]

# Shared structured logger; extra={} kwargs land in log records as JSON fields
# consumed by the metrics/dashboard pipeline (see metrics/logger.py).
logger = get_logger("fault_injector")


@dataclass
class InjectionRecord:
    r"""
    Audit entry for ONE process_plaintext() call (only appended while enabled).

    Collected into FaultInjector.history; lets post-run analysis reconstruct
    exactly which packet was hit and how hard, complementing the aggregate
    counters (which only answer "how much in total").

    ----------
    Attributes
    ----------
    packet_index : int
        1-based ordinal of this call among ALL enabled calls since injector
        construction (or last reset_stats()). Identifies the packet within
        the session; correlates with metrics logs of the same index.
    total_bits : int
        UNIT: bits (= 8 * len(data)). Denominator of the per-packet BER
        fraction; accumulated into FaultInjector.total_bits.
    bit_errors : int
        UNIT: bits flipped this packet. Always 0 when simulated_drop is True
        (a dropped packet skips bit-error injection entirely).
    simulated_drop : bool
        True if this packet was counted as a drop (data delivered as zeros of
        equal length). False means normal delivery with optional bit errors.
    """

    packet_index: int
    total_bits: int
    bit_errors: int
    simulated_drop: bool


class FaultInjector:
    r"""
    Plaintext-level fault simulator for the classical channel.

    Lifecycle (as wired in scripts/run_benchmark.py):
        injector = FaultInjector(config)          # once per scenario
        ...
        corrupted = injector.process_plaintext(seg)   # per classical segment,
        send_classical_segment(corrupted, aes_key, dc) # BEFORE encryption
        ...
        record = build_record(..., fault_injector=injector)  # snapshot metrics

    State ownership:
        - Config-derived knobs (enabled, *_probability) are fixed at
          construction; changing them requires a new instance.
        - Counters (_total_*, _error_bits, _history) accumulate across calls;
          read via the public properties; cleared only by reset_stats().
        - _rng is deliberately a private random.Random instance, NOT the
          module-global RNG: seeding it (tests do) cannot perturb randomness
          elsewhere in the protocol simulation.

    When disabled (default): process_plaintext() returns input unchanged and
    touches no state, the rest of the system sees an identity function.
    """

    def __init__(self, config: dict) -> None:
        """
        Build an injector from the global experiment config dict.

        ----------
        Parameters
        ----------
        config : dict
            Full experiment config. Only the nested "fault_injection" key is
            read; absent key or absent sub-keys fall back to safe defaults:
            disabled, zero BER, zero PLR (fail-safe: doing nothing).

        ---------------------
        Attributes (set here)
        ---------------------
        enabled : bool
            Master gate. False short-circuits every other code path.
        bit_error_probability : float
            UNIT: probability/bit in [0,1]. P(any given bit is XOR-flipped).
        packet_loss_probability : float
            UNIT: probability/packet in [0,1]. P(packet counted as dropped).
            Checked BEFORE bit injection; mutually exclusive per packet.
        _rng : random.Random
            Isolated stream for all injection decisions. Constructed WITHOUT a
            seed (system entropy) so production runs are non-reproducible by
            design; tests may call self._rng.seed(n) for determinism.
        """
        fi = config.get("fault_injection", {})
        self.enabled: bool = fi.get("enabled", False)
        self.bit_error_probability: float = fi.get("bit_error_probability", 0.0)
        self.packet_loss_probability: float = fi.get("packet_loss_probability", 0.0)
        self._rng = random.Random()
        self._reset_counters()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_plaintext(self, data: bytes) -> bytes:
        r"""
        Maybe corrupt plaintext before encryption/send.

        ----------
        Parameters
        ----------
        data : bytes
            One classical-channel segment (plaintext, pre-encryption).
            Length is preserved by every outcome branch (see Returns).

        -------
        Returns
        -------
        bytes
            Always len(data) bytes:
              - disabled            -> `data` unchanged (identity; zero cost)
              - dropped (simulated) -> zeros of len(data) (see below)
              - otherwise           -> `data` with 0..8*len(data) bits flipped

        ---------------
        Order of Checks
        ---------------
        Drop decision FIRST: a dropped packet undergoes no bit injection, so a
        packet contributes to either PLR or BER, never both, matching how a
        real PHY loses a whole frame before any bit-level corruption matters.
        """
        if not self.enabled:
            return data

        self._total_packets += 1
        idx = self._total_packets
        total_bits = len(data) * 8
        self._total_bits += total_bits

        # --- Packet drop decision (simulated; data still delivered) ---
        # A real lossy channel would swallow the frame, but on TCP that would
        # block Bob's recv() forever. We keep the framing intact and deliver
        # zeros instead: Phase 6's byte-for-byte echo comparison then sees a
        # full-width mismatch and patches the range, the observable behaviour
        # matches "packet lost, then recovered by reroute".
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
            return bytes(len(data))

        # --- Bit error injection ---
        # Independent coin flip per bit: buf[i] ^= (1 << bit) flips exactly the
        # sampled bit position, so expected errors = total_bits * p and the
        # realized per-packet rate concentrates around p (binomial). This is a
        # Binary Symmetric Channel, the same model snr_db assumes in reverse.
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
        """Clear counters and history (use between test/scenario runs so
        metrics from one run never leak into the next)."""
        self._reset_counters()

    # ------------------------------------------------------------------
    # Derived metrics
    # ------------------------------------------------------------------
    # Read-only views over the private counters. metrics/collector.py polls
    # these once at end-of-run to populate the TransmissionRecord; nothing
    # inside this module acts on them.

    @property
    def total_packets(self) -> int:
        """UNIT: packets (enabled process_plaintext() calls). PLR denominator."""
        return self._total_packets

    @property
    def total_bits(self) -> int:
        """UNIT: bits summed over all processed packets. BER denominator."""
        return self._total_bits

    @property
    def error_bits(self) -> int:
        """UNIT: bits actually flipped across the session. BER numerator."""
        return self._error_bits

    @property
    def dropped_packets(self) -> int:
        """UNIT: packets counted as simulated drops. PLR numerator."""
        return self._dropped_packets

    @property
    def ber(self) -> float:
        r"""
        Bit Error Rate = error_bits / total_bits.

        UNIT: dimensionless ratio in [0, 1] (errors per transmitted bit);
        conventionally quoted as e.g. "BER = 1e-3".

        Converges to bit_error_probability as total_bits grows (law of large
        numbers); finite-sample deviation is what test_fault_injector.py
        bounds statistically. Edge case: no traffic yet -> 0.0 (neutral
        default so downstream math never divides by zero).
        """
        if self._total_bits == 0:
            return 0.0
        return self._error_bits / self._total_bits

    @property
    def plr(self) -> float:
        r"""
        Packet Loss Rate = dropped_packets / total_packets.

        UNIT: dimensionless ratio in [0, 1] (dropped per sent packet).

        SIMULATED metric: packets are still delivered (as zeros) to avoid TCP
        deadlock, this number reports what PLR *would* be on a real lossy
        channel configured with packet_loss_probability. Edge case: no
        packets yet -> 0.0.
        """
        if self._total_packets == 0:
            return 0.0
        return self._dropped_packets / self._total_packets

    @property
    def snr_db(self) -> float | None:
        r"""
        Simulated SNR derived from BER via the Binary Symmetric Channel model:

            SNR_dB = 10 * log10((1 - BER) / BER)

        UNIT: decibels (dB), rounded to 2 places. Interpretation: treat the
        channel as BSC with crossover p = BER; (1-p)/p is the effective
        signal-to-noise power ratio, expressed in dB by convention.

        DERIVED metric, not physically measured, consistent with plr being
        simulated. Monotonically decreasing in BER, which is the property
        tests assert (higher injected error rate -> lower reported SNR).

        Edge cases:
            BER == 0 -> None  (perfect channel => infinite SNR; None is the
                       JSON-safe stand-in for infinity used by the collector)
            BER >= 1 -> -math.inf  (every bit wrong; channel carries no info)
        """
        p = self.ber
        if p <= 0.0:
            return None
        if p >= 1.0:
            return -math.inf
        return round(10.0 * math.log10((1.0 - p) / p), 2)

    @property
    def history(self) -> list[InjectionRecord]:
        """Shallow copy of all InjectionRecords (oldest first). Copy prevents
        callers from mutating the audit trail behind the counters' back."""
        return list(self._history)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _reset_counters(self) -> None:
        """Zero all accumulators. Called once by __init__ and by reset_stats();
        the single place counter semantics are defined."""
        self._total_packets = 0
        self._dropped_packets = 0
        self._total_bits = 0
        self._error_bits = 0
        self._history: list[InjectionRecord] = []
