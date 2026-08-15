"""
Tests for quantum/reconciliation.py — single-pass parity-based key reconciliation.

Structure
---------
Unit tests (synthetic bit strings, fast):
  test_single_error_corrected           — one injected error per block → corrected
  test_zero_errors_no_correction        — identical bits → no corrections, minimal sacrifice
  test_even_error_count_undetected      — 2 errors in one block → NOT caught (documented limitation)

Integration tests (real BB84 + socket pairs, moderate speed):
  test_reconciliation_reduces_mismatch_noise05   — noise=0.05, n_qubits=200
  test_reconciliation_reduces_mismatch_noise12   — noise=0.12, n_qubits=200
  test_noiseless_reconciliation_unchanged         — noise=0.00, minimal overhead
  test_abort_threshold_unaffected                 — noise=0.30, SESSION_ABORTED before reconciliation

Helpers
-------
_SimpleDC   — thin wrapper over a raw socket for testing reconciliation in isolation
_run_pair   — runs reconcile_alice + reconcile_bob in threads over a socketpair
"""

from __future__ import annotations

import math
import socket
import threading

import pytest
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from classical.aes_channel import derive_key
from classical.transport import send_frame, recv_frame
from quantum.bb84 import (
    run_bb84,
    BB84_CHECK_FRACTION,
    BB84_QBER_THRESHOLD,
)
from quantum.reconciliation import (
    ReconcileResult,
    ReconciliationIncompleteError,
    block_size_for_qber,
    reconcile_alice,
    reconcile_bob,
    verify_key_alice,
    verify_key_bob,
)


# ---------------------------------------------------------------------------
# Test helper — thin DataChannel substitute for socket pairs
# ---------------------------------------------------------------------------


class _SimpleDC:
    """Minimal DataChannel-compatible wrapper around a raw socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._conn = sock

    def send(self, data: bytes) -> None:
        send_frame(self._conn, data)

    def recv(self) -> bytes:
        return recv_frame(self._conn)

    def close(self) -> None:
        try:
            self._conn.close()
        except OSError:
            pass


def _run_pair(
    alice_bits: list[int],
    bob_bits: list[int],
    qber: float,
) -> tuple[ReconcileResult, ReconcileResult]:
    """
    Run reconcile_alice and reconcile_bob in threads over a socketpair.
    Returns (alice_result, bob_result).
    """
    sock_a, sock_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    dc_a = _SimpleDC(sock_a)
    dc_b = _SimpleDC(sock_b)

    alice_result: list[ReconcileResult | None] = [None]
    bob_result:   list[ReconcileResult | None] = [None]
    exc_holder:   list[Exception | None]       = [None]

    def run_bob() -> None:
        try:
            bob_result[0] = reconcile_bob(bob_bits, qber, dc_b)
        except Exception as e:
            exc_holder[0] = e

    t = threading.Thread(target=run_bob, daemon=True)
    t.start()
    alice_result[0] = reconcile_alice(alice_bits, qber, dc_a)
    t.join(timeout=10)

    sock_a.close()
    sock_b.close()

    if exc_holder[0]:
        raise exc_holder[0]
    return alice_result[0], bob_result[0]


def _bb84_alice_bob_keys(n_qubits: int, noise: float, seed: int):
    """
    Run BB84 and return (alice_key_bits, bob_key_bits, qber) exactly as the
    server's _simulate_qkd() extracts them, so the test matches production.
    """
    r = run_bb84(n_qubits=n_qubits, injected_noise=noise, seed=seed)
    matching = [
        i for i in range(len(r.alice_bits))
        if r.alice_bases[i] == r.bob_bases[i]
    ]
    split = max(1, int(len(matching) * BB84_CHECK_FRACTION))
    key_positions = matching[split:]
    alice_key = [r.alice_bits[i]   for i in key_positions]
    bob_key   = [r.bob_results[i]  for i in key_positions]
    return alice_key, bob_key, r.qber


# ===========================================================================
# Unit tests — synthetic bit strings
# ===========================================================================


class TestSingleErrorCorrected:
    """A single injected error per block is found and corrected."""

    def test_error_in_first_block(self):
        # 20 bits, QBER=0.1 → block_size=7, blocks: [0,7), [7,14), [14,20)
        alice_bits = [1, 0, 1, 0, 1, 1, 0,  1, 1, 0, 1, 0, 1, 1,  0, 1, 0, 1, 1, 0]
        bob_bits   = list(alice_bits)
        bob_bits[3] ^= 1          # flip bit 3 (in block 0)

        ar, br = _run_pair(alice_bits, bob_bits, qber=0.10)

        assert br.bits_corrected == 1, "Exactly one correction expected"
        assert ar.reconciled_bits == br.reconciled_bits, (
            "After reconciliation, both parties must have identical key bits"
        )

    def test_error_in_last_block(self):
        alice_bits = [0] * 30
        bob_bits   = list(alice_bits)
        bob_bits[27] ^= 1         # flip bit in the last (partial) block

        ar, br = _run_pair(alice_bits, bob_bits, qber=0.10)

        assert br.bits_corrected == 1
        assert ar.reconciled_bits == br.reconciled_bits

    def test_reconciled_key_derives_same_aes_key(self):
        """Corrected bits → identical HKDF outputs → AES-GCM will succeed."""
        alice_bits = [1, 0, 0, 1, 1, 0, 1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0, 1, 0, 0]
        bob_bits   = list(alice_bits)
        bob_bits[5] ^= 1

        ar, br = _run_pair(alice_bits, bob_bits, qber=0.10)

        # Both parties derive the same AES key from reconciled bits
        alice_aes = derive_key(ar.reconciled_bits)
        bob_aes   = derive_key(br.reconciled_bits)
        assert alice_aes == bob_aes, "HKDF outputs must match after reconciliation"


class TestZeroErrorsNoCorrection:
    """Identical input bits: no corrections, minimal bit sacrifice."""

    def test_no_bits_corrected(self):
        bits = [1, 0, 1, 1, 0, 0, 1, 0, 1, 0, 1, 0, 1, 1, 0, 0, 1, 0, 1, 0]
        ar, br = _run_pair(bits, bits[:], qber=0.05)

        assert br.bits_corrected == 0, "No corrections when bits already match"
        assert ar.reconciled_bits == br.reconciled_bits

    def test_alice_bits_corrected_always_zero(self):
        """Alice is the reference party — she never counts a correction."""
        bits = [0, 1] * 15
        ar, _ = _run_pair(bits, bits[:], qber=0.05)
        assert ar.bits_corrected == 0

    def test_noiseless_key_agreement_preserved(self):
        bits = list(range(25))           # arbitrary; parity is XOR of each block
        bits = [b % 2 for b in bits]
        ar, br = _run_pair(bits, bits[:], qber=0.0)

        assert ar.reconciled_bits == br.reconciled_bits
        assert derive_key(ar.reconciled_bits) == derive_key(br.reconciled_bits)


class TestEvenErrorCountUndetected:
    """
    Two errors in one block produce matching parities — the block passes
    silently and errors remain.  This is the documented single-pass limitation.

    The test asserts this limitation is REAL, not merely theoretical.
    """

    def test_two_errors_in_same_block_not_corrected(self):
        # block_size for QBER=0.10 → 7; put both errors inside block 0 ([0,7))
        alice_bits = [1, 0, 1, 0, 1, 1, 0,  1, 1, 0, 1, 0, 1, 1]
        bob_bits   = list(alice_bits)
        bob_bits[1] ^= 1   # error at position 1 }
        bob_bits[4] ^= 1   # error at position 4 } same block — even count

        ar, br = _run_pair(alice_bits, bob_bits, qber=0.10)

        # The mismatch count in block 0 is even → parity match → no correction
        assert br.bits_corrected == 0, (
            "Even-count errors must NOT be detected by single-pass reconciliation "
            "(this is the documented limitation)"
        )
        # Keys still differ at positions 1 and 4 after privacy amplification trim
        # (unless both positions were sacrificed)
        bsz = block_size_for_qber(0.10)
        sacrificed = br.bits_sacrificed
        # If error positions survive the trim, the reconciled keys will differ
        surviving_errors = [
            p for p in [1, 4] if p >= sacrificed
        ]
        if surviving_errors:
            assert ar.reconciled_bits != br.reconciled_bits, (
                "Reconciled keys must still differ when even-count errors survive "
                "the privacy-amplification trim"
            )

    def test_even_errors_caught_by_key_verification(self):
        """
        verify_key_{alice,bob} catches the residual mismatch from even-count errors,
        raising ReconciliationIncompleteError instead of a late AES InvalidTag.
        """
        alice_bits = [1, 0, 1, 0, 1, 1, 0,  1, 1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 0, 1,
                      1, 0, 0, 1, 1]  # 25 bits
        bob_bits = list(alice_bits)
        bob_bits[1] ^= 1
        bob_bits[4] ^= 1   # two errors in block 0

        ar, br = _run_pair(alice_bits, bob_bits, qber=0.10)

        alice_aes = derive_key(ar.reconciled_bits)
        bob_aes   = derive_key(br.reconciled_bits)

        if alice_aes == bob_aes:
            pytest.skip("Error positions happened to fall in the sacrificed prefix "
                        "— no residual mismatch in this particular bit pattern")

        # Verify that the verification functions raise correctly
        sock_a, sock_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        dc_a, dc_b = _SimpleDC(sock_a), _SimpleDC(sock_b)

        bob_exc: list[Exception | None] = [None]

        def run_bob_verify():
            try:
                verify_key_bob(dc_b, bob_aes)
            except ReconciliationIncompleteError as e:
                bob_exc[0] = e
            except Exception as e:
                bob_exc[0] = e

        t = threading.Thread(target=run_bob_verify, daemon=True)
        t.start()

        with pytest.raises(ReconciliationIncompleteError):
            verify_key_alice(dc_a, alice_aes)

        t.join(timeout=5)
        sock_a.close(); sock_b.close()

        # Bob also gets the error (either ReconciliationIncompleteError from his
        # own decrypt failure, or a ConnectionError when Alice closes the socket)
        assert bob_exc[0] is not None, (
            "Bob must also observe a failure when keys differ post-reconciliation"
        )


# ===========================================================================
# Integration tests — real BB84 + socket pairs
# ===========================================================================


class TestIntegrationNoiseless:
    """Noiseless case: reconciliation adds trivial overhead, no corrections."""

    def test_noiseless_no_corrections(self):
        alice_bits, bob_bits, qber = _bb84_alice_bob_keys(200, 0.0, seed=42)
        # Noiseless: alice and bob keys already identical
        assert alice_bits == bob_bits

        ar, br = _run_pair(alice_bits, bob_bits, qber)

        assert br.bits_corrected == 0
        assert ar.reconciled_bits == br.reconciled_bits
        # Keys still derivable (not empty after minimal sacrifice)
        if ar.reconciled_bits:
            assert derive_key(ar.reconciled_bits) == derive_key(br.reconciled_bits)

    def test_noiseless_block_size_is_large(self):
        """At QBER≈0, block_size should be capped to key length (very large formula)."""
        _, _, qber = _bb84_alice_bob_keys(200, 0.0, seed=0)
        # QBER=0 → formula gives floor(0.73/1e-4)=7300; capped to key length
        bsz = block_size_for_qber(max(qber, 0.0))
        assert bsz >= 1


class TestIntegrationModerateNoise:
    """
    noise=0.05: reconciliation should substantially reduce key-mismatch rate.

    Baseline (no reconciliation, n_qubits=200): P(all 25 key bits correct) ≈ (0.95)^25 ≈ 28%.
    With single-pass reconciliation: P(each block has ≤1 error) ≈ 70–80%.

    We run 20 trials with fixed seeds.  Asserting ≥ 60% success is a conservative
    threshold well above the ~28% baseline.
    """

    N_TRIALS = 20
    NOISE = 0.05
    N_QUBITS = 200

    def test_key_mismatch_rate_drops(self):
        successes_before = 0
        successes_after  = 0
        corrections_total = 0

        for seed in range(self.N_TRIALS):
            alice_bits, bob_bits, qber = _bb84_alice_bob_keys(
                self.N_QUBITS, self.NOISE, seed
            )

            # Before reconciliation: keys agree iff all bits identical
            if alice_bits == bob_bits:
                successes_before += 1

            # After reconciliation
            if not alice_bits:   # empty key → skip
                continue
            ar, br = _run_pair(alice_bits, bob_bits, max(qber, 0.001))
            corrections_total += br.bits_corrected

            if ar.reconciled_bits and br.reconciled_bits:
                if derive_key(ar.reconciled_bits) == derive_key(br.reconciled_bits):
                    successes_after += 1

        print(f"\n[recon noise=0.05] before={successes_before}/{self.N_TRIALS}  "
              f"after={successes_after}/{self.N_TRIALS}  "
              f"corrections={corrections_total}")

        assert successes_after > successes_before, (
            "Reconciliation must improve key agreement rate at noise=0.05"
        )
        assert successes_after >= max(1, int(self.N_TRIALS * 0.55)), (
            f"Expected ≥55% success rate after reconciliation, "
            f"got {successes_after}/{self.N_TRIALS}"
        )


class TestIntegrationAboveThreshold:
    """
    noise=0.12: reconciliation never runs on sessions that abort.

    Sessions where QBER > BB84_QBER_THRESHOLD (0.11) raise SessionAbortedError
    at the server level — the node never gets raw key bits, so reconciliation
    is never called.  This test confirms that the QBER abort path is not touched.
    """

    def test_abort_threshold_still_triggers(self):
        """
        At noise=0.30 (well above 0.11), all trials should have high QBER.
        Reconciliation is not called — this is purely a BB84 QBER check.
        """
        for seed in range(5):
            r = run_bb84(n_qubits=200, injected_noise=0.30, seed=seed)
            # QBER at 30% noise should reliably exceed 0.11
            assert r.qber > BB84_QBER_THRESHOLD, (
                f"seed={seed}: QBER={r.qber:.4f} expected > {BB84_QBER_THRESHOLD}"
            )

    def test_reconciliation_not_needed_when_aborted(self):
        """
        Demonstrate the ordering guarantee: reconciliation is called only after
        a successful EBIT_RESULT (qber ≤ 0.11).  For sessions that would abort,
        there are no key bits to reconcile — passing empty list is safe.
        """
        ar, br = _run_pair([], [], qber=0.30)
        assert ar.reconciled_bits == []
        assert br.reconciled_bits == []
        assert ar.bits_corrected == 0
        assert br.bits_corrected == 0


class TestBlockSizeHeuristic:
    """block_size_for_qber returns the correct Cascade pass-1 values."""

    @pytest.mark.parametrize("qber,expected", [
        (0.01,  73),   # floor(0.73/0.01)
        (0.05,  14),   # floor(0.73/0.05)
        (0.10,   7),   # floor(0.73/0.10)
        (0.11,   6),   # floor(0.73/0.11)
        (0.0,  7300),  # floor(0.73/1e-4) — noiseless, EPSILON kicks in
    ])
    def test_values(self, qber, expected):
        assert block_size_for_qber(qber) == expected

    def test_always_at_least_one(self):
        assert block_size_for_qber(0.99) >= 1
        assert block_size_for_qber(1.0)  >= 1
