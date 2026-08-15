# Dynamic Hybrid Quantum-Classical Communication System — Project TODO

Source: minor project proposal (BB84/E91 QKD + Superdense Coding, dynamic payload split,
echo-based adaptive rerouting). Decision: **no QuNetSim** — build directly on Qiskit for
quantum primitives and plain Python classes for the network layer, to avoid dependency risk
from an inactive library.

Work through phases in order. Each phase should be built and unit-tested in isolation before
the next phase touches it, per the proposal's own methodology (§6.1).

---

## Phase 0 — Setup
- [ ] Repo scaffold: `server/`, `node/`, `quantum/`, `classical/`, `metrics/`, `tests/`, `config/`
- [ ] Python 3.10+ venv, pin Qiskit version, `requirements.txt`
- [ ] Config file: protocol selection (BB84/E91), security level, payload size, ebit count, noise/QBER injection level
- [ ] Basic logging setup (structured JSON, per §6.5)

## Phase 1 — Quantum primitives (Qiskit, standalone + unit tested)
- [ ] BB84: state prep, basis choice, measurement, sifting, QBER calculation (Eq. 1)
- [ ] E91: entangled pair measurement at multiple angles, CHSH parameter S (Eq. 2)
- [ ] Superdense coding: Bell state encode/decode, verify 2 bits/qubit, 100% decode accuracy in noiseless case
- [ ] Unit tests: known-input/known-output for each, plus QBER-under-injected-noise sanity check

## Phase 2 — Network layer
- [ ] Ebit Server: node registry, EPR pair generation/distribution, connection request validation
- [ ] Node A / Node B classes: discovery, connection handshake, holding qubits from ebit pairs
- [ ] Wire in Phase 1 primitives so A/B can actually run BB84/E91 over the "quantum channel"

## Phase 3 — Classical channel
- [ ] AES-256 encrypt/decrypt using the QKD-derived key (PyCryptodome or `cryptography`)
- [ ] TCP socket transport between Node A and Node B
- [ ] Unit test: encrypt→transmit→decrypt round-trip with a known key

## Phase 4 — Dynamic Split Controller (core contribution — most important module)
- [ ] Implement decision logic per Fig. 4: sufficient ebits? → QBER within threshold (~11% BB84)? → security level (low/medium/high) → payload-vs-ebit-availability → output Q%/C% split ratio
- [ ] Unit tests covering each branch of the flowchart independently
- [ ] Make thresholds config-driven, not hardcoded, so Phase 7 benchmarking can sweep them

## Phase 5 — Dual-channel transmission + reassembly
- [ ] Partition payload per split ratio, send quantum portion via SDC, classical portion via AES-256+socket, concurrently
- [ ] Node B reassembly of both portions into full payload

## Phase 6 — Echo validation + adaptive rerouting
- [ ] Node B echoes reassembled payload back to Node A
- [ ] Node A compares original vs echo, identifies mismatched segments
- [ ] Dual-indicator logic (Fig. 5): high QBER → channel issue → reroute; low QBER + mismatch → flag possible eavesdropper → reroute + log
- [ ] Retransmit affected segments over classical channel only, re-verify via echo, abort+report on repeat failure

## Phase 7 — Metrics & logging
- [ ] Structured JSON logs: QBER, CHSH, SKR, split ratio decisions, BER, SNR, throughput, latency, PLR, echo mismatch events
- [ ] Get this schema right early — it's what the evaluation section (§6.8–6.9) and any plots depend on

## Phase 8 — Integration, security validation, benchmarking
- [ ] End-to-end pipeline test: discovery → QKD → split → dual transmission → echo → (reroute if needed)
- [ ] Simulated eavesdropper node — confirm QBER elevation + echo mismatch reliably detect it
- [ ] Sweep noise levels, payload sizes, security levels; log outcomes for the report's results section

---

## Working notes
- Each phase = its own scoped Claude Code prompt, diagnostic-before-fixing.
- Verify claims (esp. QBER/split logic) against actual test output, not just Claude Code's self-report.
- Matplotlib/Plotly dashboard for metrics can come after Phase 7 data exists — don't build it against fake data.
