"""Tests for superdense coding."""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from quantum.superdense_coding import encode_and_decode, run_superdense_coding_all


def test_all_messages_decode_correctly():
    """All four 2-bit messages must decode with 100% accuracy in noiseless simulation."""
    results = run_superdense_coding_all()
    print("\n[SDC noiseless] Results:")
    for r in results:
        print(f"  sent={r.sent}  received={r.received}  success={r.success}")
    assert all(r.success for r in results), (
        "Superdense coding failed for: "
        + str([r for r in results if not r.success])
    )


def test_00_decodes():
    r = encode_and_decode(0, 0)
    assert r.received == (0, 0)


def test_01_decodes():
    r = encode_and_decode(0, 1)
    assert r.received == (0, 1)


def test_10_decodes():
    r = encode_and_decode(1, 0)
    assert r.received == (1, 0)


def test_11_decodes():
    r = encode_and_decode(1, 1)
    assert r.received == (1, 1)
