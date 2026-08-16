# Dynamic Hybrid Quantum-Classical Communication System

A simulation of a hybrid network protocol that combines Quantum Key Distribution
(QKD) with AES-256-GCM classical encryption, routing payload segments
simultaneously over a quantum channel (via superdense coding) and a classical
channel (via AES-GCM over TCP). The split between channels adapts dynamically to
channel quality (QBER), security level, and available entangled bit (ebit) count.

> **Simulation scope**: all quantum operations run inside Qiskit's AerSimulator
> on a single machine. There is no physical quantum hardware and no actual
> quantum network. The system models the protocol layer faithfully; the quantum
> circuits are real Qiskit circuits with proper Bell-state encoding and
> measurement, but they execute locally.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Deployment Model](#3-deployment-model)
4. [Project Structure](#4-project-structure)
5. [Setup](#5-setup)
6. [Configuration](#6-configuration)
7. [Running the System](#7-running-the-system)
8. [Inspecting Results](#8-inspecting-results)
9. [Known Limitations](#9-known-limitations)
10. [Bugs Found and Fixed During Development](#10-bugs-found-and-fixed-during-development)
11. [Tech Stack](#11-tech-stack)

---

## 1. Project Overview

The system implements a six-phase pipeline for each payload transfer:

| Phase | What happens |
|-------|-------------|
| 1 | Quantum primitives: BB84 QKD, E91 entanglement check, superdense coding |
| 2 | Network layer: Ebit Server brokers node registry and QKD; Nodes connect |
| 3 | Classical channel: AES-256-GCM transport with 4-byte length-prefixed framing |
| 4 | Dynamic Split Controller: decides how much payload goes quantum vs. classical |
| 5 | Dual-channel transmission: quantum and classical segments sent concurrently |
| 6 | Echo validation + adaptive rerouting: Node B echoes payload back; mismatches are patched |
| 7 | Metrics: structured JSON logging of every transfer (QBER, SKR, throughput, latency …) |
| 9 | Key reconciliation: single-pass parity-based error correction before HKDF key derivation |

**Why hybrid?** QKD alone is bandwidth-limited by qubit availability. Classical AES
alone lacks information-theoretic security guarantees. The hybrid model uses QKD to
protect the most security-sensitive portion of the payload and AES-256-GCM for the
remainder, with the split fraction driven by a runtime decision tree that responds
to the current channel's QBER and the caller's declared security level.

**Why Qiskit, not QuNetSim?** Stated in `TODO.md`: *"no QuNetSim — build directly
on Qiskit for quantum primitives and plain Python classes for the network layer, to
avoid dependency risk from an inactive library."* QuNetSim is deliberately excluded
from `requirements.txt` and `environment.yml`.

---

## 2. Architecture

### Entities

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Ebit Server                                │
│  - Node registry (name → socket)                                   │
│  - Connection request routing (Alice → Bob handshake)              │
│  - QKD simulation: runs BB84/E91 circuits, distributes key halves  │
│  - Retry logic (MAX_RETRIES=2 before SESSION_ABORTED)              │
│  - Listens on: config[server_port]  (default 5000)                │
└────────────────┬────────────────────────────┬───────────────────────┘
                 │  JSON-over-TCP             │  JSON-over-TCP
                 │  (control plane only)      │  (control plane only)
         ┌───────┴──────┐               ┌─────┴───────┐
         │   Node A     │               │   Node B    │
         │  (Alice)     │               │   (Bob)     │
         └──────────────┘               └─────────────┘
                 │                             │
                 │   Direct A↔B connections    │
                 ├──── data_port (TCP) ────────┤
                 │    AES-GCM classical seg    │
                 │    + metadata frame         │
                 │    + echo / patch frames    │
                 │    + reconciliation frames  │
                 │                             │
                 └──── quantum_port (TCP) ─────┘
                      SDC-encoded quantum seg
```

**Important**: actual payload data never travels through the Ebit Server. The
server's role is limited to the control plane (registry, handshake, QKD
distribution). Once QKD completes, Node A and Node B communicate directly via two
independent TCP connections (`data_port` and `quantum_port`), both Bob-listens /
Alice-connects.

### Per-transfer flow

```
Node A (Alice)                    Ebit Server              Node B (Bob)
──────────────                    ───────────              ────────────
connect() + register()    ──►     registry
                                  ──────────────────────►  notify_connect
                          ◄──     CONNECT_ACCEPTED         ACCEPT
                   ┌─────────────────────────────────────────────────┐
                   │              QKD phase                           │
                   │  Alice: REQUEST_EBITS                            │
                   │  Server runs BB84/E91 circuits                  │
                   │  Server sends EBIT_RESULT to both nodes         │
                   └─────────────────────────────────────────────────┘
                   ┌── Direct A↔B (data_port + quantum_port) ────────┐
                   │  1. Reconciliation: parity exchange + bisection  │
                   │     → corrected key bits                         │
                   │  2. HKDF-SHA256 key derivation                  │
                   │  3. Key verification ping/pong                  │
                   │  4. Split controller → SplitDecision            │
                   │  5. Concurrent dual-channel send:               │
                   │     quantum_port ← SDC-encoded HEAD             │
                   │     data_port   ← AES-GCM TAIL + metadata      │
                   │  6. Bob reassembles, echoes full payload back   │
                   │  7. Alice verifies echo, patches mismatches     │
                   └─────────────────────────────────────────────────┘
```

### Split Controller decision tree

```
available_ebits < 4  ───────────────────────────────►  100% classical
                                                         (EBIT_INSUFFICIENT)
QBER > 0.11  ───────────────────────────────────────►  100% classical
                                                         (QBER_EXCEEDED)
security = "low"    ────────────────────────────────►  25% quantum / 75% classical
security = "medium" ────────────────────────────────►  50% quantum / 50% classical
security = "high" + enough ebits ───────────────────►  75% quantum / 25% classical
security = "high" + ebit-constrained ───────────────►  min(ebit_capacity/payload, 75%)
```

---

## 3. Deployment Model

**Single-machine (localhost) only**, confirmed by `config/config.yaml`:

```yaml
host: "127.0.0.1"   # keep configurable; single-line change for multi-machine
```

The comment notes that multi-machine deployment would require changing the `host`
field. All three TCP endpoints (server, data channel, quantum channel) are currently
bound to `127.0.0.1`. No distributed-system setup is needed to run the full pipeline.

---

## 4. Project Structure

```
hbd_class_quant/
│
├── config/
│   └── config.yaml                  Top-level runtime configuration (all tunable values)
│
├── quantum/
│   ├── bb84.py                      BB84 QKD: qubit prep, sifting, QBER calculation
│   ├── e91.py                       E91: Bell-state correlations, CHSH parameter S
│   ├── superdense_coding.py         SDC: 2 classical bits per Bell-state operation
│   ├── eavesdropper.py              Intercept-resend Eve for security validation tests
│   └── reconciliation.py           Single-pass parity reconciliation + key verification
│
├── server/
│   └── ebit_server.py               Threaded TCP server: registry, handshake, QKD distribution
│
├── node/
│   └── node.py                      Node class (Alice or Bob role): QKD, dual-channel tx/rx
│
├── classical/
│   ├── aes_channel.py               AES-256-GCM encrypt/decrypt + HKDF-SHA256 key derivation
│   ├── transport.py                 DataChannel: TCP with 4-byte length-prefixed framing
│   └── fault_injector.py            Plaintext-level BER/PLR injector for benchmarking only
│
├── split_controller/
│   └── controller.py                compute_split(): the core hybrid decision tree (Phase 4)
│
├── transmission/
│   ├── payload_splitter.py          split_payload() + reassemble_from_segments()
│   ├── quantum_transmit.py          encode_bytes_sdc(): bytes → SDC round-trip via AerSimulator
│   ├── classical_transmit.py        send/recv wrappers around aes_channel + transport
│   └── echo_validation.py          Phase 6: echo comparison, mismatch diagnosis, rerouting
│
├── metrics/
│   ├── logger.py                    get_logger(): structured JSON output via python-json-logger
│   ├── collector.py                 MetricsCollector: writes one JSON record per transfer
│   └── logs/                        Timestamped JSONL files written by the benchmark script
│
├── scripts/
│   ├── run_benchmark.py             54-config grid sweep (3 security × 3 payload × 3 noise × 2 fault)
│   └── generate_dashboard.py        Reads JSONL logs → static PNGs + self-contained HTML dashboard
│
├── tests/
│   ├── test_bb84.py                 9 tests: BB84 correctness, QBER, noise, QBER threshold
│   ├── test_e91.py                  4 tests: CHSH bounds, correlation signs
│   ├── test_superdense_coding.py    5 tests: all 4 2-bit messages, round-trip fidelity
│   ├── test_network.py              9 tests: server registration, handshake, QKD distribution
│   ├── test_classical_channel.py    15 tests: AES-GCM, HKDF, InvalidTag, transport framing
│   ├── test_fault_injector.py       14 tests: BER/PLR injection, SNR derivation, disabled mode
│   ├── test_split_controller.py     24 tests: all decision branches, ebit-constrained path
│   ├── test_transmission.py         28 tests: SDC encoding, concurrent send, reassembly
│   ├── test_echo_validation.py      18 tests: clean pass, reroute, eavesdropper logging
│   ├── test_metrics.py              6 tests: record schema, JSONL write/read
│   ├── test_eavesdropper.py         5 tests: Eve QBER ≈ 0.25, exceeds abort threshold
│   ├── test_end_to_end.py           2 tests: full BB84 + E91 pipelines, byte-for-byte payload check
│   └── test_reconciliation.py       19 tests: single-error correction, even-error limitation, integration
│
├── environment.yml                  Conda environment definition (name: hcq_proj)
├── requirements.txt                 Pip packages (also listed in environment.yml)
└── TODO.md                          Phase-by-phase design notes and working log
```

---

## 5. Setup

### Prerequisites

- [Miniconda](https://docs.conda.io/en/latest/miniconda.html) or Anaconda
- Python 3.13 (pinned in `environment.yml`)

### Install

```bash
# Create and activate the conda environment
conda env create -f environment.yml
conda activate hcq_proj

# Install plotly (required for the dashboard; not in environment.yml)
pip install plotly
```

The environment name is `hcq_proj` as declared in `environment.yml`.

`requirements.txt` lists the same packages and can be used with `pip install -r requirements.txt` in any Python 3.13 environment instead.

---

## 6. Configuration

All runtime values are in `config/config.yaml`. The benchmark script and tests
override these programmatically; the YAML file is the source of defaults for any
code that loads it directly.

```yaml
# ── Protocol ──────────────────────────────────────────────────────────────────
protocol: "BB84"           # "BB84" | "E91"
                           # E91 runs a CHSH check then falls through to BB84 for
                           # key material (standard E91 practice)

security_level: "medium"  # "low" | "medium" | "high"
                           # Controls the quantum_fraction target (see Split section)

payload_size_bytes: 1024   # Default payload size for ad-hoc use; benchmark overrides this

available_ebits: 50        # Ebit pool size for the session; controls whether the
                           # high-security ebit-constrained path is taken

injected_qber: 0.0         # [0.0, 1.0] — probability of bit-flip per qubit (noise model)
                           # 0.0 = noiseless; ≥ 0.11 → session aborted after 2 retries

# ── Network ───────────────────────────────────────────────────────────────────
host: "127.0.0.1"          # Loopback; change to IP for multi-machine (not tested)
server_port: 5000          # Ebit Server control-plane TCP port
data_port: 5001            # Direct A↔B classical channel (Bob listens, Alice connects)
quantum_port: 5002         # Direct A↔B quantum (SDC) channel  (Bob listens, Alice connects)

n_qubits: 200              # BB84 qubits per QKD session
                           # With BB84_CHECK_FRACTION=0.75 and ~50% basis agreement,
                           # ~25 raw key bits result on average

n_pairs_per_setting: 500   # E91 only: Bell pairs measured per angle-setting combination

# ── Fault Injection ───────────────────────────────────────────────────────────
fault_injection:
  enabled: false                   # MUST stay false outside Phase 8 benchmarking
  bit_error_probability: 0.0       # Per-bit plaintext flip probability
  packet_loss_probability: 0.0     # Per-packet simulated drop probability
                                   # (see Known Limitations — PLR is simulated)

# ── Split Controller ──────────────────────────────────────────────────────────
split:
  min_viable_ebits: 4              # Below this → 100% classical (EBIT_INSUFFICIENT)
  quantum_fraction_low: 0.25       # security_level="low"
  quantum_fraction_medium: 0.50    # security_level="medium"  (default)
  max_quantum_fraction: 0.75       # Ceiling for security_level="high"
                                   # Never 1.0 — classical path must always exist
                                   # for echo/sifting traffic
  bits_per_ebit: 2                 # SDC theoretical capacity (2 classical bits per qubit)
```

### Key constants (defined in code, not overridable via YAML)

| Constant | Value | Location | Meaning |
|----------|-------|----------|---------|
| `BB84_QBER_THRESHOLD` | 0.11 | `quantum/bb84.py` | Session abort threshold |
| `BB84_CHECK_FRACTION` | 0.75 | `quantum/bb84.py` | Fraction of sifted bits used for QBER check |
| `ANOMALY_THRESHOLD` | 0.05 | `transmission/echo_validation.py` | Echo mismatch: QBER ≤ this → POSSIBLE_EAVESDROPPER |
| `EPSILON` | 1e-4 | `quantum/reconciliation.py` | QBER floor to avoid ÷0 in block-size formula |
| `MAX_RETRIES` | 2 | `server/ebit_server.py` | QKD attempts before SESSION_ABORTED |

---

## 7. Running the System

### Run all tests

```bash
conda run -n hcq_proj python -m pytest
```

All 158 tests should pass. Each test file is self-contained; integration tests
spin up their own `EbitServer` and `Node` instances with OS-assigned ephemeral ports.

```bash
# Run a specific test file
conda run -n hcq_proj python -m pytest tests/test_end_to_end.py -v

# Run just reconciliation tests
conda run -n hcq_proj python -m pytest tests/test_reconciliation.py -v
```

### Run the benchmark sweep

54 configurations: 3 security levels × 3 payload sizes (16 B / 128 B / 512 B) × 3
noise levels (0.00 / 0.05 / 0.12) × 2 fault settings (off / BER=5%).

```bash
/home/abhinavkarn/miniconda3/envs/hcq_proj/bin/python -u scripts/run_benchmark.py 2>/dev/null
```

> Use the direct Python path rather than `conda run` — `conda run` adds a buffering
> layer that suppresses per-config progress prints until the entire sweep finishes.

Expected outcomes per noise level (n_qubits=200, reconciliation enabled):

| Injected noise | Expected outcomes |
|---------------|-------------------|
| 0.00 | All 18 configs → CLEAN_PASS |
| 0.05 | 18 configs → CLEAN_PASS (reconciliation corrects residual errors) |
| 0.12 | ~9 configs → SESSION_ABORTED (QBER > 0.11 after 2 retries); remainder → RECONCILIATION_INCOMPLETE |

Total runtime: approximately 25–30 seconds.

### Generate the dashboard

```bash
conda run -n hcq_proj python scripts/generate_dashboard.py
```

Reads all `metrics/logs/benchmark_*.jsonl` files. Produces:

- `metrics/figures/fig1_qber_vs_noise.png` — measured QBER vs inferred noise level
- `metrics/figures/fig2_outcome_breakdown.png` — CLEAN_PASS / aborted / incomplete by noise
- `metrics/figures/fig3_split_ratio.png` — quantum fraction by security level
- `metrics/figures/fig5_skr_vs_qber.png` — Secret Key Rate vs QBER scatter
- `metrics/figures/fig6_throughput_latency.png` — throughput and latency by payload size
- `metrics/dashboard.html` — self-contained interactive Plotly page (4–5 MB, no server needed)

### Run a single end-to-end transfer (integration tests)

There is no standalone "run one transfer" script. The closest entry point is the
end-to-end integration test, which spins up all three entities:

```bash
conda run -n hcq_proj python -m pytest tests/test_end_to_end.py -v -s
```

This runs both BB84 and E91 full pipelines and prints QBER, SKR, split decision,
throughput, and echo outcome to stdout.

---

## 8. Inspecting Results

### Benchmark logs

Each benchmark run writes to a timestamped file:

```
metrics/logs/benchmark_YYYYMMDD_HHMMSS.jsonl
```

One JSON object per line (JSONL format). Each record contains:

```json
{
  "schema_version": "1.0",
  "session_id": "...",
  "transfer_id": "...",
  "timestamp_utc": "2026-08-16T05:50:47",
  "protocol": "BB84",
  "qber": 0.0,
  "chsh": null,
  "qkd_key_bits": 27,
  "qkd_elapsed_s": 0.1266,
  "skr_bits_per_second": 213.21,
  "split_reason": "MEDIUM_SECURITY",
  "quantum_fraction": 0.5,
  "classical_fraction": 0.5,
  "payload_bytes": 128,
  "quantum_bytes": 64,
  "classical_bytes": 64,
  "transfer_elapsed_s": 0.2431,
  "throughput_bytes_per_s": 526.6,
  "latency_s": 0.2431,
  "echo_outcome": "CLEAN_PASS",
  "echo_recovered": false,
  "mismatch_source": null,
  "echo_diagnosis": null,
  "retransmit_bytes": 0,
  "fault_injection_enabled": false,
  "ber_simulated": null,
  "snr_db_simulated": null,
  "plr_simulated": null,
  "bits_injected_errors": 0,
  "packets_simulated_dropped": 0
}
```

**Important**: only completed transfers (CLEAN_PASS, RECOVERED_VIA_REROUTE,
CHANNEL_FAILURE outcomes) produce log records. SESSION_ABORTED and
RECONCILIATION_INCOMPLETE sessions exit before `collector.record_transfer()` is
called, so they do not appear in the JSONL files.

### Structured runtime logs

All modules log to stdout in JSON format (via `python-json-logger`). Each log line
includes `asctime`, `name`, `levelname`, and the message. Example from the echo
validation module:

```json
{"asctime": "2026-08-16T05:50:48", "name": "echo_validation", "levelname": "INFO",
 "message": "Echo validation: clean first-pass match", "total_bytes": 128, "outcome": "CLEAN_PASS"}
```

Security events (echo mismatch with low QBER) use a separate `echo_validation.SECURITY`
logger at CRITICAL level — visually unmistakable in the log stream.

### Dashboard

Open `metrics/dashboard.html` directly in any web browser — no server needed.
The file is fully self-contained (Plotly.js is embedded). Hover over any point for
exact values; scroll or zoom within panels.

### Test output

```bash
conda run -n hcq_proj python -m pytest -v 2>/dev/null
```

Pass/fail per test. Tests with `-s` or `print()` in the test body (e.g.
`test_end_to_end.py`) print QBER, SKR, split decision, and echo outcome inline.

---

## 9. Known Limitations

These limitations are explicitly documented in the source code.

### Single-pass reconciliation catches only odd-count errors

`quantum/reconciliation.py` docstring (§ "Known limitation — single-pass only"):

> A block containing an **even number of errors** passes its parity check silently.
> The errors remain in the reconciled key and are NOT corrected. Full multi-pass
> Cascade with random block reshuffling between passes would catch these cases but
> is out of scope.

When the post-reconciliation key verification ping/pong fails (keys still differ),
`ReconciliationIncompleteError` is raised. This is the `RECONCILIATION_INCOMPLETE`
outcome in benchmark output. The test `test_even_error_count_undetected` in
`tests/test_reconciliation.py` explicitly verifies this behaviour.

### BER, SNR, and PLR are simulated, not physically measured

`classical/fault_injector.py` docstring:

- **BER**: actual fraction of bits flipped in plaintext by the injector; real value.
- **PLR**: *simulated* — "the fraction of packets that would have been lost on a real
  lossy channel at this probability." TCP does not actually drop the packets (doing so
  would deadlock Bob's `recv()`); instead the injector delivers zeros of the same
  length and increments the drop counter.
- **SNR (dB)**: derived proxy via the Binary Symmetric Channel model
  `SNR_dB = 10 * log10((1 - BER) / BER)`, not a physically measured value.

These are clearly labelled `_simulated` in the metrics schema and in log output.

### Quantum channel is noiseless in simulation

The `quantum_transmit.py` SDC implementation runs the full encode+decode circuit in
AerSimulator, which is noiseless by default. Bit errors in the quantum segment are
therefore impossible in normal operation — all observable corruption comes from the
classical fault injector. A real quantum channel would require a noise model in
AerSimulator.

### Maximum quantum fraction is 75%, never 100%

Documented in `split_controller/controller.py`:

> MAX_QUANTUM_FRACTION = 0.75 — ceiling for "high" security, deliberately NOT 1.0.
> The classical channel must always carry at least 25% because: (a) Phase 6 echo /
> retransmission traffic needs a classical path; (b) QKD classical sifting messages
> travel over this channel; (c) keeping 100% quantum would defeat the "hybrid" premise.

### E91 key material comes from a BB84 sub-step

`server/ebit_server.py` (`_simulate_qkd`): after the CHSH test passes, the server
runs a BB84 circuit to generate actual key bits. The CHSH test provides the security
certificate; the key bits themselves are from Z-basis measurements. This is noted as
"standard E91 practice" in the code. In the benchmark grid (which used BB84 only),
`chsh` is `null` for every logged record.

### Reconciliation channel uses a constant authentication key

`quantum/reconciliation.py` docstring (§ "Channel security model"):

> `_RECON_AUTH_KEY` is embedded in source and therefore not secret. This is acceptable
> because: (a) parity bits are discarded in privacy amplification; (b) the session key
> is not transmitted during reconciliation; (c) AES-GCM authentication protects against
> corruption and replay. Using a constant auth key avoids the chicken-and-egg problem
> of deriving an auth key from the (not-yet-agreed-upon) session bits.

### Simplified privacy amplification

After reconciliation, `parity_bits_revealed` bits are discarded from the front of
the key. The docstring notes: *"a rigorous implementation uses a universal hash
function (e.g., Toeplitz) sized to the actual Shannon leakage. The approximation is
acceptable here; the remaining bits are further processed by HKDF-SHA256."*

---

## 10. Bugs Found and Fixed During Development

The git log has two commits (`First commit` and `Benchmarking`). Details of earlier
development history (before these commits) are not available in the repository.
The following bugs are verifiable from comments in the current source or from
explicit test assertions.

### Bug 1 — Basis-asymmetric noise model in BB84

**What happened**: the original noise injection applied only an X gate (bit-flip),
which causes errors in Z-basis measurements but is transparent in X-basis
(X|+⟩ = |+⟩). This made measured QBER depend on which basis was used, so the
check-sample QBER did not reliably track the injected noise level.

**Fix** (verifiable in `quantum/bb84.py`, lines 85–98): the noise model now applies
both an X gate (probability `injected_noise`) *and* an independent Z gate
(probability `injected_noise`). The X gate errors in Z-basis, the Z gate errors in
X-basis, giving symmetric QBER ≈ `injected_noise` regardless of basis. The
multi-line comment in `run_bb84()` explains the per-basis error probability algebra.

### Bug 2 — Socket hang when one side raises before completing its protocol turn

**What happened**: when Bob's AES-GCM decryption raised `InvalidTag` (due to a key
mismatch caused by residual QKD bit errors), Bob's exception propagated before
sending any response. Alice was left blocked on `dc.recv()` waiting for Bob's echo
or pong, with no timeout. This caused the entire benchmark run to hang.

**Fix**: two complementary changes:
1. In `scripts/run_benchmark.py` (`run_b()` exception handler, lines ~196–212):
   `c_dc.close()` and `q_dc.close()` are called explicitly in every exception path
   so Alice's `recv()` sees a connection-closed error instead of blocking forever.
   The comment reads: *"Explicitly close data channels so Alice's dc.recv() unblocks
   immediately instead of hanging until the per-config timeout. This is the critical
   fix for the key-mismatch (InvalidTag) hang."*
2. In `quantum/reconciliation.py` (`verify_key_alice` / `verify_key_bob`): both
   verification functions wrap their bodies in `except Exception`. Before raising,
   each side sends `encrypt(b"hbd-verify-fail", _RECON_AUTH_KEY)` to unblock the
   peer's `recv()`. If even that send fails, the socket is explicitly closed.

### Bug 3 — Empty payload for 512 B benchmark config

**What happened**: the benchmark script originally generated payloads using
`bytes(range(psz % 256)) * (psz // 256 + 1)`. For `psz = 512`,
`psz % 256 == 0` → `range(0)` → `bytes(range(0))` → `b""`. All 512-byte
configurations silently transmitted zero bytes.

**Fix** (verifiable in `scripts/run_benchmark.py`, line 340):
`payload = bytes(i % 256 for i in range(psz))` — generates exactly `psz` bytes
(0x00, 0x01, …, 0xFF, 0x00, …) with no dependency on modular arithmetic.
An `assert len(payload) == psz` guard on the next line catches any future regression.

### Bug 4 — Bisection depth could theoretically loop forever

Not a bug that occurred in practice, but a defensive guard added after diagnosis:
`quantum/reconciliation.py` includes `max_depth = ceil(log2(max(e-s, 2))) + 2`
with a `RuntimeError` if the bisection loop exceeds it. This is documented in the
module as "hard cap: log2(block) + slack" and is tested to never trigger on any
real BB84 key under any noise level.

---

## 11. Tech Stack

### Pinned dependencies

From `environment.yml` and `requirements.txt`:

| Package | Version | Role |
|---------|---------|------|
| Python | 3.13 | Runtime |
| qiskit | 2.5.2 | Quantum circuit construction and transpilation |
| qiskit-aer | 0.17.2 | Local quantum circuit simulator (AerSimulator) |
| numpy | 2.5.1 | Numerical operations in E91 / BB84 |
| scipy | 1.18.0 | Listed as dependency; not directly called in current source |
| pyyaml | 6.0.2 | Config file loading |
| python-json-logger | 3.3.0 | Structured JSON log formatting |
| cryptography | 49.0.0 | AES-256-GCM, HKDF-SHA256 (via `cryptography.hazmat`) |
| pytest | 8.3.5 | Test runner |
| matplotlib | 3.11.1 | Static figure generation (installed in env; not in yml) |
| plotly | 6.9.0 | Interactive HTML dashboard (install separately with `pip install plotly`) |

> `matplotlib` and `plotly` are used only by `scripts/generate_dashboard.py`.
> `pytest-timeout` is used by `tests/test_reconciliation.py` (`pytestmark = pytest.mark.timeout(30)`);
> install with `pip install pytest-timeout` if that plugin is not already present.

### Explicitly excluded

**QuNetSim** is not used and not listed anywhere in the dependency files.
Reason stated in `TODO.md`: *"no QuNetSim — build directly on Qiskit for quantum
primitives and plain Python classes for the network layer, to avoid dependency risk
from an inactive library."*

### Transport layer

Plain Python `socket` (stdlib). No asyncio, no Twisted, no gRPC. Threading is
used in two places:
- `EbitServer`: one daemon thread per connected node.
- `Node.transmit_payload` / `receive_payload`: two threads per transfer (quantum
  and classical channels run concurrently). Qiskit's AerSimulator releases the GIL
  via compiled C extensions, giving genuine parallelism for the quantum work.
