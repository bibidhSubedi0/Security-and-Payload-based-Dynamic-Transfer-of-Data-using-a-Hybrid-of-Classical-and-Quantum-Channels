# Demo: first working end-to-end hybrid QC transfer

## What this run proves

This run demonstrates the full hybrid quantum-classical pipeline working
end-to-end on `main`: two nodes register with the Ebit Server, complete a clean
BB84 QKD exchange (QBER = 0.0, 26-bit key on the first attempt), derive matching
AES-256-GCM keys via HKDF, and then split a 26-byte payload 13/13 across a
"quantum" channel (superdense coding in Qiskit AerSimulator — locally simulated,
no hardware) and a classical channel (AES-256-GCM over TCP), transmit both
segments concurrently, and reassemble byte-for-byte. Bob's echo of the reassembled
payload matches Alice's original on the first pass (`echo_outcome=CLEAN_PASS`),
and a complete metrics record (QBER, SKR, split decision, throughput, latency)
is written. In other words: QKD key exchange + dynamic hybrid payload split
both worked correctly in a single clean transfer.

## Screenshot-worthy stats

- **Single clean transfer:** `echo_outcome=CLEAN_PASS`, `QBER=0.0`,
  split `MEDIUM_SECURITY` 13 B quantum / 13 B classical, throughput `353.4 B/s`
- **Benchmark sweep:** `40/54` configs `CLEAN_PASS`, `0` errors, `0` timeouts
  (5 SESSION_ABORTED + 9 RECONCILIATION_INCOMPLETE, all from the noise=0.12 grid row)
- **Full test suite:** `158/158` tests passing

See `demo/demo_run_output.txt` for the full captured console output of the
single clean transfer.

## Dashboard

Regenerated from all benchmark logs (`269` records across `9` files):

- Interactive HTML: `metrics/dashboard.html` (self-contained, no server needed)
- Static figures: `metrics/figures/fig1_qber_vs_noise.png`,
  `fig2_outcome_breakdown.png`, `fig3_split_ratio.png`,
  `fig5_skr_vs_qber.png`, `fig6_throughput_latency.png`

Open `metrics/dashboard.html` in any browser.

## Reproduce

```bash
# 1. Environment (Python 3.14 in this repo's .venv; pins target 3.13)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt matplotlib plotly pytest-timeout

# 2. Run the full test suite
pytest tests/ -v                       # 158/158 passed

# 3. Single clean end-to-end BB84 transfer (captures console output)
python -m pytest tests/test_end_to_end.py::test_e2e_bb84_full_pipeline -v -s

# 4. 54-config benchmark sweep (writes metrics/logs/benchmark_*.jsonl)
python scripts/run_benchmark.py

# 5. Regenerate the dashboard from the benchmark logs
python scripts/generate_dashboard.py
```

## Context

- All quantum operations are simulated in Qiskit's AerSimulator on a single
  machine (localhost) — there is no physical quantum hardware or network.
- This demo added no changes to core logic (quantum/, classical/,
  split_controller/, transmission/, server/, node/); it only packages run
  outputs, generated figures, and this summary.