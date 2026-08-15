"""
Phase 7b — Fault injector tests.

Structure
---------
1. Off-by-default: FaultInjector with no fault_injection config key → disabled,
   process_plaintext() is identity.
2. Disabled: enabled=false explicitly → same identity behaviour.
3. Bit-error rate: with fixed seed and known probability × large payload,
   observed BER is within 30% of configured rate (probabilistic tolerance).
4. Packet-drop rate: with fixed seed and known probability × many calls,
   observed PLR is within 30% of configured rate.
5. SNR derived from BER: verify formula and edge cases (BER=0 → None, BER>0 → finite).
6. No-change guarantee with Phase 5/6 test: run an isolated round-trip
   (classical segment send → recv) with injector DISABLED and confirm
   received bytes == sent bytes (existing behaviour unaffected).
7. Fault injector present-but-disabled does not affect a full classical
   segment round-trip (integration smoke test).
"""

import socket
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from classical.fault_injector import FaultInjector, InjectionRecord
from classical.aes_channel import derive_key
from classical.transport import send_frame, recv_frame
from quantum.bb84 import run_bb84
from transmission.classical_transmit import send_classical_segment, recv_classical_segment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _aes_key() -> bytes:
    r = run_bb84(n_qubits=200, injected_noise=0.0, seed=42)
    return derive_key(r.sifted_key)


def _disabled_config() -> dict:
    return {}   # no fault_injection key at all → defaults to disabled


def _enabled_config(ber: float = 0.0, plr: float = 0.0) -> dict:
    return {
        "fault_injection": {
            "enabled": True,
            "bit_error_probability": ber,
            "packet_loss_probability": plr,
        }
    }


class _SimpleDC:
    def __init__(self, sock):
        self._conn = sock
    def send(self, data):
        send_frame(self._conn, data)
    def recv(self):
        return recv_frame(self._conn)
    def close(self):
        self._conn.close()


# ---------------------------------------------------------------------------
# 1. Off-by-default
# ---------------------------------------------------------------------------

def test_off_by_default_no_config_key():
    injector = FaultInjector({})          # no fault_injection key
    assert injector.enabled is False
    data = b"hello world"
    assert injector.process_plaintext(data) == data


def test_off_by_default_explicit_false():
    injector = FaultInjector({"fault_injection": {"enabled": False}})
    assert injector.enabled is False
    data = b"test payload"
    assert injector.process_plaintext(data) == data


# ---------------------------------------------------------------------------
# 2. Disabled: stats stay zero
# ---------------------------------------------------------------------------

def test_disabled_stats_stay_zero():
    injector = FaultInjector(_disabled_config())
    for _ in range(10):
        injector.process_plaintext(b"A" * 100)
    assert injector.total_packets == 0
    assert injector.error_bits == 0
    assert injector.ber == 0.0
    assert injector.plr == 0.0
    assert injector.snr_db is None


# ---------------------------------------------------------------------------
# 3. Bit-error rate tracks configured probability
# ---------------------------------------------------------------------------

def test_ber_tracks_configured_rate():
    """
    With bit_error_probability=0.10 and a large enough payload,
    the observed BER should be within 30% of 0.10 (i.e. [0.07, 0.13]).
    """
    BER_TARGET = 0.10
    injector = FaultInjector(_enabled_config(ber=BER_TARGET))
    injector._rng.seed(0)   # deterministic for the test

    payload = b"\xAA" * 5000   # 40000 bits — large enough for law of large numbers
    corrupted = injector.process_plaintext(payload)

    observed_ber = injector.ber
    print(f"\n[ber_test] target={BER_TARGET:.3f}  observed={observed_ber:.4f}  "
          f"errors={injector.error_bits}/{injector.total_bits}")

    assert 0.07 <= observed_ber <= 0.13, (
        f"BER {observed_ber:.4f} is more than 30% off from target {BER_TARGET}"
    )
    # Verify some bytes were actually changed
    assert corrupted != payload


def test_ber_zero_means_no_errors():
    """bit_error_probability=0.0 → corrupted == original, error_bits=0."""
    injector = FaultInjector(_enabled_config(ber=0.0, plr=0.0))
    data = b"unchanged" * 50
    result = injector.process_plaintext(data)
    assert result == data
    assert injector.error_bits == 0
    assert injector.ber == 0.0


# ---------------------------------------------------------------------------
# 4. Packet-drop rate tracks configured probability
# ---------------------------------------------------------------------------

def test_plr_tracks_configured_rate():
    """
    With packet_loss_probability=0.20 and 200 packets,
    observed PLR should be within 40% of 0.20 (i.e. [0.12, 0.28]).
    Wider tolerance than BER because packet count is smaller.
    """
    PLR_TARGET = 0.20
    injector = FaultInjector(_enabled_config(plr=PLR_TARGET))
    injector._rng.seed(1)

    for _ in range(200):
        injector.process_plaintext(b"packet" * 10)

    observed_plr = injector.plr
    print(f"\n[plr_test] target={PLR_TARGET:.3f}  observed={observed_plr:.4f}  "
          f"dropped={injector.dropped_packets}/{injector.total_packets}")

    assert 0.12 <= observed_plr <= 0.28, (
        f"PLR {observed_plr:.4f} is more than 40% off from target {PLR_TARGET}"
    )


def test_plr_zero_means_no_drops():
    injector = FaultInjector(_enabled_config(ber=0.0, plr=0.0))
    for _ in range(20):
        injector.process_plaintext(b"data")
    assert injector.dropped_packets == 0
    assert injector.plr == 0.0


# ---------------------------------------------------------------------------
# 5. SNR derivation
# ---------------------------------------------------------------------------

def test_snr_is_none_when_ber_zero():
    injector = FaultInjector(_enabled_config(ber=0.0))
    injector.process_plaintext(b"A" * 100)
    assert injector.snr_db is None


def test_snr_is_finite_and_positive_for_low_ber():
    """Low BER → high SNR (positive dB)."""
    injector = FaultInjector(_enabled_config(ber=0.01))
    injector._rng.seed(2)
    injector.process_plaintext(b"B" * 10000)
    snr = injector.snr_db
    print(f"\n[snr_test] BER≈0.01 → SNR={snr} dB")
    assert snr is not None
    assert snr > 0.0   # (1-0.01)/0.01 ≈ 99 → ~20 dB


def test_snr_decreases_as_ber_increases():
    """Higher BER should produce lower SNR."""
    low_ber_inj  = FaultInjector(_enabled_config(ber=0.01))
    high_ber_inj = FaultInjector(_enabled_config(ber=0.20))
    for inj in [low_ber_inj, high_ber_inj]:
        inj._rng.seed(3)
        inj.process_plaintext(b"C" * 10000)
    low_snr  = low_ber_inj.snr_db
    high_snr = high_ber_inj.snr_db
    print(f"\n[snr_order] BER=0.01→{low_snr:.1f}dB  BER=0.20→{high_snr:.1f}dB")
    assert low_snr > high_snr


# ---------------------------------------------------------------------------
# 6. Identity round-trip with injector disabled
# ---------------------------------------------------------------------------

def test_disabled_injector_classical_roundtrip():
    """
    Disabled injector must not affect a send→recv round-trip over a socket.
    This mirrors what Phase 5/6 does with the classical channel.
    """
    injector = FaultInjector(_disabled_config())
    aes_key  = _aes_key()
    plaintext = b"Phase 5/6 classical segment - must be unchanged" * 4

    sock_a, sock_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    dc_a = _SimpleDC(sock_a)
    dc_b = _SimpleDC(sock_b)

    # Simulate what orchestration does when injector is disabled:
    # process_plaintext is identity → same bytes go to send_classical_segment
    to_send = injector.process_plaintext(plaintext)
    assert to_send == plaintext   # verified identity

    send_classical_segment(to_send, aes_key, dc_a)
    received = recv_classical_segment(dc_b, aes_key)

    sock_a.close()
    sock_b.close()

    assert received == plaintext
    assert injector.error_bits == 0
    assert injector.total_packets == 0
    print(f"\n[disabled_roundtrip] {plaintext[:30]!r}... ✓")


# ---------------------------------------------------------------------------
# 7. Enabled injector produces actual corruption in the plaintext
# ---------------------------------------------------------------------------

def test_enabled_injector_classical_roundtrip_shows_corruption():
    """
    With bit errors enabled, the bytes Bob decrypts differ from what Alice
    originally sent (since corruption was injected into plaintext before
    encryption).  This is the mechanism Phase 6's echo comparison exploits.
    """
    injector = FaultInjector(_enabled_config(ber=0.15))  # high rate for reliable detection
    injector._rng.seed(99)
    aes_key   = _aes_key()
    plaintext = b"data to be corrupted by injector" * 20   # 640 bytes = 5120 bits

    sock_a, sock_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    dc_a = _SimpleDC(sock_a)
    dc_b = _SimpleDC(sock_b)

    corrupted = injector.process_plaintext(plaintext)
    send_classical_segment(corrupted, aes_key, dc_a)
    received  = recv_classical_segment(dc_b, aes_key)

    sock_a.close()
    sock_b.close()

    # Bob receives the corrupted version (not the original)
    assert received == corrupted
    assert received != plaintext, (
        "Injector was enabled at 15% BER but no corruption was observed — very unlikely"
    )
    assert injector.error_bits > 0
    print(f"\n[enabled_roundtrip] error_bits={injector.error_bits}  "
          f"ber={injector.ber:.4f}  corrupted={received != plaintext}")


# ---------------------------------------------------------------------------
# 8. reset_stats clears counters
# ---------------------------------------------------------------------------

def test_reset_stats():
    injector = FaultInjector(_enabled_config(ber=0.1))
    injector._rng.seed(5)
    injector.process_plaintext(b"X" * 1000)
    assert injector.total_bits > 0

    injector.reset_stats()
    assert injector.total_packets == 0
    assert injector.error_bits == 0
    assert injector.dropped_packets == 0
    assert injector.history == []


# ---------------------------------------------------------------------------
# 9. InjectionRecord history is populated correctly
# ---------------------------------------------------------------------------

def test_history_populated():
    injector = FaultInjector(_enabled_config(ber=0.05))
    injector._rng.seed(7)
    data = b"A" * 100
    for _ in range(3):
        injector.process_plaintext(data)
    assert len(injector.history) == 3
    for i, rec in enumerate(injector.history):
        assert isinstance(rec, InjectionRecord)
        assert rec.packet_index == i + 1
        assert rec.total_bits == 800   # 100 bytes × 8
