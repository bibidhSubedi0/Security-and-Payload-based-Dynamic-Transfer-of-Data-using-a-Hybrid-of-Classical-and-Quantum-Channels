import { useState, useEffect, useMemo } from "react"
import "./Test.css"

const PARAMETERS = [
  {
    id: "quantumDistance",
    label: "Quantum Channel Distance",
    unit: "km",
    type: "range",
    min: 10,
    max: 200,
    step: 10,
    default: 50,
    explanation: "Physical distance of the quantum fiber link. Longer distances increase photon loss and decoherence, reducing key rate and increasing QBER.",
    affects: ["Key Rate", "QBER", "Secure Distance"]
  },
  {
    id: "qber",
    label: "Quantum Bit Error Rate (QBER)",
    unit: "%",
    type: "range",
    min: 0.5,
    max: 11,
    step: 0.5,
    default: 2.5,
    explanation: "Fraction of erroneous bits in the sifted key. Caused by detector noise, misalignment, and eavesdropping. Above ~11% (BB84), secure key extraction becomes impossible.",
    affects: ["Secure Key Rate", "Privacy Amplification", "Abort Decision"]
  },
  {
    id: "detectorEfficiency",
    label: "Detector Efficiency",
    unit: "%",
    type: "range",
    min: 10,
    max: 95,
    step: 5,
    default: 60,
    explanation: "Probability a single-photon detector registers a photon. Higher efficiency directly increases raw key rate and maximum achievable distance.",
    affects: ["Raw Key Rate", "Max Distance", "Key Generation Time"]
  },
  {
    id: "darkCountRate",
    label: "Dark Count Rate",
    unit: "Hz",
    type: "range",
    min: 10,
    max: 10000,
    step: 50,
    default: 100,
    explanation: "Spurious detector clicks without photons. Major contributor to QBER, especially at long distances where signal is weak. Lower is better.",
    affects: ["QBER", "Secure Key Rate", "Max Distance"]
  },
  {
    id: "classicalBandwidth",
    label: "Classical Channel Bandwidth",
    unit: "Gbps",
    type: "range",
    min: 0.1,
    max: 100,
    step: 0.1,
    default: 10,
    explanation: "Throughput of the classical channel for encrypted payload transfer. Must exceed application data rate. Higher bandwidth enables larger payloads and faster transfer.",
    affects: ["Payload Throughput", "Transfer Time", "Max Payload Size"]
  },
  {
    id: "classicalLatency",
    label: "Classical Channel Latency",
    unit: "ms",
    type: "range",
    min: 0.1,
    max: 100,
    step: 0.5,
    default: 2,
    explanation: "Round-trip time for classical acknowledgments and synchronization. Affects protocol overhead for error correction and privacy amplification rounds.",
    affects: ["Protocol Latency", "Key Reconciliation Time", "Real-time Suitability"]
  },
  {
    id: "payloadSize",
    label: "Payload Size",
    unit: "MB",
    type: "range",
    min: 1,
    max: 10000,
    step: 10,
    default: 100,
    explanation: "Amount of data to transfer. Larger payloads require more key material (one-time pad) or longer AES-GCM sessions. Impacts classical channel utilization.",
    affects: ["Transfer Time", "Key Consumption", "Classical Channel Load"]
  },
  {
    id: "payloadSensitivity",
    label: "Payload Sensitivity",
    unit: "",
    type: "select",
    options: [
      { value: "low", label: "Low (Standard encryption)" },
      { value: "medium", label: "Medium (Forward secrecy required)" },
      { value: "high", label: "High (Information-theoretic security)" },
      { value: "critical", label: "Critical (Quantum-only, no classical fallback)" }
    ],
    default: "medium",
    explanation: "Security requirement level. Determines whether classical encryption (AES) suffices or quantum one-time pad is mandatory. Higher sensitivity demands more key material.",
    affects: ["Key Consumption Rate", "Channel Selection", "Fallback Policy"]
  },
  {
    id: "switchThreshold",
    label: "Dynamic Switch Threshold (QBER)",
    unit: "%",
    type: "range",
    min: 3,
    max: 10,
    step: 0.5,
    default: 7,
    explanation: "QBER threshold triggering fallback from quantum to classical channel. Below threshold: quantum keys refresh continuously. Above: system pauses quantum key gen, uses stored keys.",
    affects: ["Channel Switching", "Key Freshness", "Availability"]
  },
  {
    id: "keyBufferSize",
    label: "Key Buffer Size",
    unit: "MB",
    type: "range",
    min: 10,
    max: 1000,
    step: 10,
    default: 200,
    explanation: "Pre-generated quantum key reserve. Larger buffer sustains payload encryption during quantum channel outages. Consumes storage but improves availability.",
    affects: ["Outage Resilience", "Continuous Operation", "Storage Cost"]
  }
]

const RESULTS = [
  {
    id: "rawKeyRate",
    label: "Raw Key Rate",
    unit: "kbps",
    description: "Bits per second after sifting, before error correction. Determined by detector efficiency, distance, and pulse rate. Higher = faster key generation.",
    interpretation: (v) => v > 1000 ? "Excellent - real-time key refresh possible" : v > 100 ? "Good - suitable for most applications" : v > 10 ? "Moderate - batch key generation needed" : "Poor - distance too long or detectors too noisy"
  },
  {
    id: "secureKeyRate",
    label: "Secure Key Rate",
    unit: "kbps",
    description: "Final secret key rate after error correction and privacy amplification. This is the actual usable key material for encryption.",
    interpretation: (v) => v > 500 ? "Excellent - high-throughput QKD" : v > 50 ? "Good - practical for VPN/file encryption" : v > 5 ? "Moderate - suitable for key wrapping" : "Insufficient - consider trusted nodes or shorter distance"
  },
  {
    id: "maxDistance",
    label: "Maximum Secure Distance",
    unit: "km",
    description: "Farthest distance where secure key rate > 0. Beyond this, QBER exceeds 11% threshold and privacy amplification yields zero key.",
    interpretation: (v) => v > 150 ? "Excellent - metro/regional deployment" : v > 100 ? "Good - campus/metro area" : v > 50 ? "Moderate - building/campus" : "Limited - lab/short-haul only"
  },
  {
    id: "transferTime",
    label: "Payload Transfer Time",
    unit: "s",
    description: "Time to transmit payload over classical channel at given bandwidth, including protocol overhead for encryption/authentication.",
    interpretation: (v) => v < 1 ? "Instantaneous - real-time capable" : v < 10 ? "Fast - interactive use" : v < 60 ? "Moderate - batch transfer" : "Slow - consider compression or higher bandwidth"
  },
  {
    id: "keyConsumption",
    label: "Key Consumption Rate",
    unit: "kbps",
    description: "Rate at which quantum key material is consumed for payload encryption. One-time pad requires 1:1 key:data. AES-GCM uses key for session setup only.",
    interpretation: (v, params) => {
      const sensitivity = params.payloadSensitivity
      if (sensitivity === "critical" || sensitivity === "high") {
        return v > 0 ? "Matches payload rate - one-time pad active" : "No key available - transfer blocked"
      }
      return v > 0 ? "Session keys derived - efficient" : "No quantum key - fallback to classical"
    }
  },
  {
    id: "availability",
    label: "Channel Availability",
    unit: "%",
    description: "Probability the hybrid system can transfer data at any moment. Accounts for quantum outages, classical failures, and buffer depletion.",
    interpretation: (v) => v > 99.9 ? "Five nines - carrier grade" : v > 99 ? "High - enterprise suitable" : v > 95 ? "Moderate - acceptable for non-critical" : "Low - requires redundancy"
  },
  {
    id: "securityLevel",
    label: "Effective Security Level",
    unit: "bits",
    description: "Information-theoretic security bits provided. For OTP: key length. For computational: AES key strength (128/256) reduced by side-channel risks.",
    interpretation: (v, params) => {
      if (params.payloadSensitivity === "critical") return `${v}-bit information-theoretic (OTP)`
      if (params.payloadSensitivity === "high") return `${v}-bit information-theoretic (OTP)`
      return `${v}-bit computational (AES-${v})`
    }
  },
  {
    id: "recommendation",
    label: "System Recommendation",
    unit: "",
    description: "Automated configuration advice based on current parameters.",
    interpretation: (v) => v
  }
]

function calculateResults(params) {
  const {
    quantumDistance,
    qber,
    detectorEfficiency,
    darkCountRate,
    classicalBandwidth,
    classicalLatency,
    payloadSize,
    payloadSensitivity,
    switchThreshold,
    keyBufferSize
  } = params

  const distanceLoss = Math.exp(-quantumDistance / 22)
  const rawRate = 1000 * detectorEfficiency / 100 * distanceLoss * (1 - qber / 100)
  const errorCorrectionOverhead = 1.1 + qber / 50
  const privacyAmplification = Math.max(0, 1 - qber / 11)
  const secureRate = Math.max(0, rawRate / errorCorrectionOverhead * privacyAmplification)

  const maxDist = -22 * Math.log(11 / (100 * (1 + darkCountRate / 10000) * (100 / detectorEfficiency)))

  const transferTime = (payloadSize * 8) / (classicalBandwidth * 1000) + classicalLatency / 1000 * 3

  const isOTP = payloadSensitivity === "high" || payloadSensitivity === "critical"
  const keyConsumption = isOTP ? (payloadSize * 8 * 1000) / Math.max(transferTime, 0.001) : 128

  const quantumAvailability = Math.max(0, 1 - qber / switchThreshold) * 0.999
  const classicalAvailability = 0.9999
  const bufferHours = keyBufferSize * 8000 / Math.max(keyConsumption, 1)
  const bufferFactor = Math.min(1, bufferHours / 24)
  const availability = (quantumAvailability * 0.7 + classicalAvailability * 0.3) * (0.5 + 0.5 * bufferFactor) * 100

  const securityLevel = isOTP ? Math.min(256, keyBufferSize * 8) : 256

  let recommendation = "Configuration OK"
  if (qber > switchThreshold) recommendation = "⚠ QBER exceeds switch threshold - quantum channel paused, using buffered keys"
  else if (secureRate < keyConsumption && isOTP) recommendation = "⚠ Secure key rate < consumption - buffer will deplete"
  else if (maxDist < quantumDistance) recommendation = "⚠ Distance exceeds maximum secure range - no key generation possible"
  else if (availability < 95) recommendation = "⚠ Low availability - increase key buffer or improve classical channel"
  else if (secureRate > 100 && availability > 99) recommendation = "✓ Optimal - high secure rate with excellent availability"

  return {
    rawKeyRate: Math.round(rawRate),
    secureKeyRate: Math.round(secureRate),
    maxDistance: Math.round(Math.max(0, maxDist)),
    transferTime: Math.round(transferTime * 100) / 100,
    keyConsumption: Math.round(keyConsumption),
    availability: Math.round(availability * 10) / 10,
    securityLevel,
    recommendation
  }
}

const CHART_W = { x0: 40, x1: 300, y0: 150, y1: 10, maxDist: 200, maxRate: 700 }

function distToX(d) {
  return CHART_W.x0 + (Math.min(d, CHART_W.maxDist) / CHART_W.maxDist) * (CHART_W.x1 - CHART_W.x0)
}

function rateToY(r) {
  return CHART_W.y0 - (Math.min(r, CHART_W.maxRate) / CHART_W.maxRate) * (CHART_W.y0 - CHART_W.y1)
}

function keyRateCurve(params) {
  const pts = []
  for (let d = 0; d <= CHART_W.maxDist; d += 10) {
    const loss = Math.exp(-d / 22)
    const raw = 1000 * params.detectorEfficiency / 100 * loss * (1 - params.qber / 100)
    const secure = Math.max(0, raw / (1.1 + params.qber / 50) * Math.max(0, 1 - params.qber / 11))
    pts.push(`${distToX(d)},${rateToY(secure)}`)
  }
  return pts.join(" ")
}

function gaugeAngle(t) {
  const a = Math.PI * (1 - Math.min(Math.max(t, 0), 1))
  return 160 + 110 * Math.cos(a) * 0.92
}

function gaugeAngleY(t) {
  const a = Math.PI * (1 - Math.min(Math.max(t, 0), 1))
  return 150 - 110 * Math.sin(a) * 0.92
}

function gaugeAngle2(t) {
  const a = Math.PI * (1 - Math.min(Math.max(t, 0), 1))
  return 160 + 110 * Math.cos(a)
}

function gaugeAngleY2(t) {
  const a = Math.PI * (1 - Math.min(Math.max(t, 0), 1))
  return 150 - 110 * Math.sin(a)
}

function gaugeArc(t) {
  const a = Math.PI * (1 - Math.min(Math.max(t, 0), 1))
  const x = 160 + 110 * Math.cos(a)
  const y = 150 - 110 * Math.sin(a)
  return `M 50 150 A 110 110 0 0 1 ${x} ${y}`
}

function barWidth(value, params) {
  const max = Math.max(1000, value, params.payloadSize)
  return Math.min((value / max) * 250, 250)
}

function noiseToQber(params, noise) {
  const base = params.qber * 0.4
  const dark = (params.darkCountRate / 10000) * 4
  const dist = (params.quantumDistance / 200) * 3
  return Math.min(12, base + dark + dist + noise * 0.35)
}

function qberNoiseCurve(params) {
  const pts = []
  for (let n = 0; n <= 50; n += 5) {
    const x = 40 + (n / 50) * 260
    const y = 150 - (noiseToQber(params, n) / 12) * 140
    pts.push(`${x},${Math.max(10, y)}`)
  }
  return pts.join(" ")
}

const OUTCOME_BUCKETS = [
  { label: "0-3%", secure: 0.95, discard: 0.04, abort: 0.01 },
  { label: "3-7%", secure: 0.7, discard: 0.25, abort: 0.05 },
  { label: "7-11%", secure: 0.25, discard: 0.5, abort: 0.25 },
  { label: ">11%", secure: 0, discard: 0.3, abort: 0.7 },
]

function splitRatioFor(level) {
  const map = {
    low: { quantum: 0.3, classical: 0.7 },
    medium: { quantum: 0.55, classical: 0.45 },
    high: { quantum: 0.8, classical: 0.2 },
    critical: { quantum: 1.0, classical: 0 },
  }
  return map[level] || map.medium
}

function throughputFor(size, params) {
  const time = (size * 8) / (params.classicalBandwidth * 1000) + (params.classicalLatency / 1000) * 3
  return size / Math.max(time, 0.001)
}

function payloadToX(size) {
  return 40 + (Math.log10(Math.max(size, 1)) / Math.log10(10000)) * 260
}

function throughputToY(t) {
  const maxT = 90
  return 150 - (Math.min(t, maxT) / maxT) * 140
}

function latencyToY(sec) {
  const maxS = 72
  return 150 - (Math.min(sec, maxS) / maxS) * 140
}

function throughputCurve(params) {
  const pts = []
  for (let i = 0; i <= 40; i++) {
    const size = Math.pow(10, (i / 40) * 4)
    pts.push(`${payloadToX(size)},${throughputToY(throughputFor(size, params))}`)
  }
  return pts.join(" ")
}

function latencyCurve(params) {
  const pts = []
  for (let i = 0; i <= 40; i++) {
    const size = Math.pow(10, (i / 40) * 4)
    const time = (size * 8) / (params.classicalBandwidth * 1000) + (params.classicalLatency / 1000) * 3
    pts.push(`${payloadToX(size)},${latencyToY(time)}`)
  }
  return pts.join(" ")
}

export default function Test() {
  const [params, setParams] = useState(() => {
    const saved = localStorage.getItem("testParams")
    if (saved) return JSON.parse(saved)
    return PARAMETERS.reduce((acc, p) => ({ ...acc, [p.id]: p.default }), {})
  })
  const [activeParam, setActiveParam] = useState(null)
  const [animateResults, setAnimateResults] = useState(false)

  useEffect(() => {
    localStorage.setItem("testParams", JSON.stringify(params))
    setAnimateResults(false)
    setTimeout(() => setAnimateResults(true), 50)
  }, [params])

  const results = useMemo(() => calculateResults(params), [params])

  const handleChange = (id, value) => {
    setParams(prev => ({ ...prev, [id]: value }))
  }

  return (
    <div className={`test-page ${animateResults ? "animate" : ""}`}>
      <div className="petals" aria-hidden="true">
        {[...Array(15)].map((_, i) => (
          <span
            key={i}
            className="petal"
            style={{
              "--i": i,
              "--delay": `${i * 200}ms`,
              "--duration": `${15 + Math.random() * 10}s`,
              "--size": `${6 + Math.random() * 10}px`,
              "--drift": `${(Math.random() - 0.5) * 80}px`,
              "--rotate": `${Math.random() * 360}deg`,
              "--sway": `${(Math.random() - 0.5) * 40}px`,
            }}
          />
        ))}
      </div>

      <header className="test-header">
        <nav className="nav">
          <a href="#/" className="nav-brand">
            <span className="nav-icon">⚛</span>
            <span className="nav-text">QuantumTransfer</span>
          </a>
          <div className="nav-links">
            <a href="#/" className="nav-link">Introduction</a>
            <span className="nav-link active">Test Parameters</span>
          </div>
        </nav>
      </header>

      <main className="test-main">
        <div className="test-grid">
          <section className="params-panel" aria-labelledby="params-title">
            <header className="panel-header">
              <h2 id="params-title">Test Parameters</h2>
              <p className="panel-subtitle">Adjust quantum/classical channel properties and payload requirements</p>
            </header>

            <div className="param-groups">
              <div className="param-group">
                <h3 className="group-title">Quantum Channel</h3>
                {PARAMETERS.filter(p => ["quantumDistance", "qber", "detectorEfficiency", "darkCountRate"].includes(p.id)).map(param => (
                  <ParamControl
                    key={param.id}
                    param={param}
                    value={params[param.id]}
                    onChange={handleChange}
                    isActive={activeParam === param.id}
                    onFocus={() => setActiveParam(param.id)}
                    onBlur={() => setActiveParam(null)}
                  />
                ))}
              </div>

              <div className="param-group">
                <h3 className="group-title">Classical Channel</h3>
                {PARAMETERS.filter(p => ["classicalBandwidth", "classicalLatency"].includes(p.id)).map(param => (
                  <ParamControl
                    key={param.id}
                    param={param}
                    value={params[param.id]}
                    onChange={handleChange}
                    isActive={activeParam === param.id}
                    onFocus={() => setActiveParam(param.id)}
                    onBlur={() => setActiveParam(null)}
                  />
                ))}
              </div>

              <div className="param-group">
                <h3 className="group-title">Payload & Policy</h3>
                {PARAMETERS.filter(p => ["payloadSize", "payloadSensitivity", "switchThreshold", "keyBufferSize"].includes(p.id)).map(param => (
                  <ParamControl
                    key={param.id}
                    param={param}
                    value={params[param.id]}
                    onChange={handleChange}
                    isActive={activeParam === param.id}
                    onFocus={() => setActiveParam(param.id)}
                    onBlur={() => setActiveParam(null)}
                  />
                ))}
              </div>
            </div>
          </section>

          <section className="results-panel" aria-labelledby="results-title">
            <header className="panel-header">
              <h2 id="results-title">Results & Interpretation</h2>
              <p className="panel-subtitle">Real-time computed metrics with plain-language explanations</p>
            </header>

            <div className="results-grid">
              {RESULTS.filter(r => r.id !== "recommendation").map((result, idx) => (
                <ResultCard
                  key={result.id}
                  result={result}
                  value={results[result.id]}
                  params={params}
                  index={idx}
                  animate={animateResults}
                />
              ))}
            </div>

            <div className={`recommendation-box ${results.recommendation.startsWith("⚠") ? "warning" : "good"}`}>
              <span className="rec-label">System Recommendation</span>
              <span className="rec-message">{results.recommendation}</span>
            </div>

            <div className="legend">
              <h4>Legend</h4>
              <div className="legend-items">
                <span className="legend-item good">✓ Excellent / Good</span>
                <span className="legend-item warn">⚠ Warning / Attention</span>
                <span className="legend-item info">ℹ Informational</span>
              </div>
            </div>

            <section className="figures-panel" aria-labelledby="figures-title">
              <header className="panel-header">
                <h3 id="figures-title">Dynamic Statistics</h3>
                <p className="panel-subtitle">Visual figures computed live from current parameters</p>
              </header>

              <div className="figures-grid">
                <figure className="figure-card">
                  <figcaption className="figure-caption">Key Rate vs Distance</figcaption>
                  <svg viewBox="0 0 320 180" className="figure-svg" role="img" aria-label="Line chart of secure key rate against distance">
                    <line x1="40" y1="150" x2="300" y2="150" className="axis" />
                    <line x1="40" y1="10" x2="40" y2="150" className="axis" />
                    <text x="170" y="172" className="axis-label" textAnchor="middle">Distance (km)</text>
                    <text x="14" y="80" className="axis-label" textAnchor="middle" transform="rotate(-90 14 80)">Key Rate (kbps)</text>
                    <polyline points={keyRateCurve(params)} className="chart-line" fill="none" />
                    <line x1={distToX(params.quantumDistance)} y1="10" x2={distToX(params.quantumDistance)} y2="150" className="marker-line" />
                    <circle cx={distToX(params.quantumDistance)} cy={rateToY(results.secureKeyRate)} r="5" className="marker-dot" />
                    <text x={Math.min(distToX(params.quantumDistance) + 8, 260)} y="24" className="marker-label">
                      {results.secureKeyRate} kbps @ {params.quantumDistance} km
                    </text>
                    <text x="44" y="164" className="tick-label">0</text>
                    <text x="284" y="164" className="tick-label">200</text>
                  </svg>
                  <p className="figure-explain">
                    Exponential photon loss means key rate falls off rapidly with fiber length.
                    The dot marks your current distance and computed secure rate. Push distance
                    past {results.maxDistance} km and the line hits zero: privacy amplification
                    can no longer squeeze secrets out of a noisy key.
                  </p>
                </figure>

                <figure className="figure-card">
                  <figcaption className="figure-caption">QBER vs Switch Threshold</figcaption>
                  <svg viewBox="0 0 320 180" className="figure-svg" role="img" aria-label="Gauge showing QBER against switch threshold">
                    <path d="M 50 150 A 110 110 0 0 1 270 150" className="gauge-track" fill="none" strokeWidth="16" />
                    <path
                      d={gaugeArc(params.qber / 11)}
                      className={`gauge-fill ${params.qber > params.switchThreshold ? "danger" : ""}`}
                      fill="none"
                      strokeWidth="16"
                      strokeLinecap="round"
                    />
                    <line
                      x1={gaugeAngle(params.switchThreshold / 11)}
                      y1={gaugeAngleY(params.switchThreshold / 11)}
                      x2={gaugeAngle2(params.switchThreshold / 11)}
                      y2={gaugeAngleY2(params.switchThreshold / 11)}
                      className="threshold-tick"
                      strokeWidth="3"
                    />
                    <text x="160" y="130" className="gauge-value" textAnchor="middle">{params.qber}%</text>
                    <text x="160" y="150" className="gauge-sub" textAnchor="middle">current QBER</text>
                    <text x="50" y="170" className="tick-label" textAnchor="middle">0%</text>
                    <text x="270" y="170" className="tick-label" textAnchor="middle">11%</text>
                  </svg>
                  <p className="figure-explain">
                    The needle shows your QBER. The vertical tick marks the switch threshold
                    ({params.switchThreshold}%). When the needle passes the tick the quantum
                    channel pauses and the system drains buffered keys instead. Past 11% the
                    BB84 protocol can no longer distill a secret key at all.
                  </p>
                </figure>

                <figure className="figure-card">
                  <figcaption className="figure-caption">Channel Load Split</figcaption>
                  <svg viewBox="0 0 320 180" className="figure-svg" role="img" aria-label="Bar chart comparing quantum key supply against payload demand">
                    <rect x="40" y="30" width={barWidth(results.secureKeyRate, params)} height="34" rx="6" className="bar bar-quantum" />
                    <text x={46 + barWidth(results.secureKeyRate, params)} y="52" className="bar-value">{results.secureKeyRate} kbps key supply</text>
                    <rect x="40" y="90" width={barWidth(results.keyConsumption, params)} height="34" rx="6" className="bar bar-demand" />
                    <text x={46 + barWidth(results.keyConsumption, params)} y="112" className="bar-value">{results.keyConsumption} kbps key demand</text>
                    <line x1="40" y1="140" x2="300" y2="140" className="axis" />
                    <text x="44" y="158" className="tick-label">0</text>
                    <text x="270" y="158" className="tick-label">1000 kbps</text>
                  </svg>
                  <p className="figure-explain">
                    Quantum key supply (green) versus encryption key demand (red). If demand
                    outruns supply the key buffer drains and transfers stall. At the current
                    sensitivity ({params.payloadSensitivity}) the system
                    {results.secureKeyRate >= results.keyConsumption ? " comfortably covers" : " cannot cover"} demand.
                  </p>
                </figure>

                <figure className="figure-card">
                  <figcaption className="figure-caption">Availability Donut</figcaption>
                  <svg viewBox="0 0 320 180" className="figure-svg" role="img" aria-label="Donut chart of hybrid channel availability">
                    <circle cx="110" cy="90" r="58" className="donut-track" fill="none" strokeWidth="18" />
                    <circle
                      cx="110" cy="90" r="58"
                      className="donut-fill"
                      fill="none"
                      strokeWidth="18"
                      strokeLinecap="round"
                      strokeDasharray={`${(results.availability / 100) * 365} 365`}
                      transform="rotate(-90 110 90)"
                    />
                    <text x="110" y="88" className="donut-value" textAnchor="middle">{results.availability}%</text>
                    <text x="110" y="106" className="donut-sub" textAnchor="middle">uptime</text>
                    <text x="190" y="60" className="donut-legend-line">Quantum: {Math.round(Math.max(0, 1 - params.qber / params.switchThreshold) * 100)}% active</text>
                    <text x="190" y="85" className="donut-legend-line">Classical: 99.99% active</text>
                    <text x="190" y="110" className="donut-legend-line">Buffer: {params.keyBufferSize} MB reserve</text>
                    <text x="190" y="135" className="donut-legend-line">{results.availability > 99 ? "Carrier grade" : results.availability > 95 ? "Acceptable" : "Needs improvement"}</text>
                  </svg>
                  <p className="figure-explain">
                    Blended uptime across both channels plus the key buffer. The quantum arc
                    shrinks as QBER approaches the switch threshold; the classical arc stays
                    near full. The buffer buys resilience: {params.keyBufferSize} MB of stored
                    key smooths over quantum outages.
                  </p>
                </figure>

                <figure className="figure-card">
                  <figcaption className="figure-caption">Measured QBER vs Injected Noise</figcaption>
                  <svg viewBox="0 0 320 180" className="figure-svg" role="img" aria-label="Line chart of measured QBER against injected eavesdropper noise">
                    <line x1="40" y1="150" x2="300" y2="150" className="axis" />
                    <line x1="40" y1="10" x2="40" y2="150" className="axis" />
                    <line x1="40" y1={150 - (11 / 12) * 140} x2="300" y2={150 - (11 / 12) * 140} className="abort-line" strokeDasharray="4 3" />
                    <text x="296" y={150 - (11 / 12) * 140 - 4} className="abort-label" textAnchor="end">11% abort limit</text>
                    <polyline points={qberNoiseCurve(params)} className="chart-line" fill="none" />
                    <line x1="40" y1={150 - (params.qber / 12) * 140} x2="300" y2={150 - (params.qber / 12) * 140} className="marker-line" />
                    <text x="300" y={150 - (params.qber / 12) * 140 - 5} className="marker-label" textAnchor="end">current {params.qber}%</text>
                    <text x="170" y="172" className="axis-label" textAnchor="middle">Injected Noise (Eve power %)</text>
                    <text x="14" y="80" className="axis-label" textAnchor="middle" transform="rotate(-90 14 80)">Measured QBER (%)</text>
                    <text x="44" y="164" className="tick-label">0</text>
                    <text x="280" y="164" className="tick-label">50</text>
                    <text x="46" y="16" className="tick-label">12</text>
                  </svg>
                  <p className="figure-explain">
                    Simulates an eavesdropper (Eve) injecting noise into the quantum channel.
                    The rising curve shows measured QBER climbing as Eve's power increases.
                    When it crosses the dashed 11% line, BB84 detects the intrusion with
                    certainty and aborts. Your horizontal line marks the configured QBER
                    ({params.qber}%) - the vertical distance to the curve is your safety margin.
                  </p>
                </figure>

                <figure className="figure-card">
                  <figcaption className="figure-caption">Outcome Breakdown by Noise Level</figcaption>
                  <svg viewBox="0 0 320 180" className="figure-svg" role="img" aria-label="Stacked bars showing secure, discarded, and aborted key fractions by noise bucket">
                    <line x1="40" y1="150" x2="300" y2="150" className="axis" />
                    {OUTCOME_BUCKETS.map((b, i) => {
                      const x = 55 + i * 62
                      const barH = 110
                      const secureH = b.secure * barH
                      const discardH = b.discard * barH
                      const abortH = b.abort * barH
                      const highlight = noiseToQber(params, i === 3 ? 45 : (i * 10 + 5)) >= [0, 3, 7, 11][i]
                      return (
                        <g key={b.label} opacity={highlight ? 1 : 0.45}>
                          <rect x={x} y={150 - secureH} width="40" height={secureH} className="seg-secure" />
                          <rect x={x} y={150 - secureH - discardH} width="40" height={discardH} className="seg-discard" />
                          <rect x={x} y={150 - secureH - discardH - abortH} width="40" height={abortH} className="seg-abort" />
                          <text x={x + 20} y="164" className="tick-label" textAnchor="middle">{b.label}</text>
                        </g>
                      )
                    })}
                    <rect x="40" y="12" width="10" height="10" className="seg-secure" />
                    <text x="54" y="21" className="tick-label">secure</text>
                    <rect x="100" y="12" width="10" height="10" className="seg-discard" />
                    <text x="114" y="21" className="tick-label">discarded</text>
                    <rect x="180" y="12" width="10" height="10" className="seg-abort" />
                    <text x="194" y="21" className="tick-label">abort</text>
                    <text x="170" y="176" className="axis-label" textAnchor="middle">Noise Bucket (QBER range)</text>
                  </svg>
                  <p className="figure-explain">
                    How sifted key material is split at each noise level: green = secure key
                    after privacy amplification, amber = bits discarded during error
                    correction, red = full protocol abort. The 0-3% bucket is your normal
                    operating zone (mostly green). Past 7% most bits get discarded and past
                    11% the protocol aborts entirely (all red).
                  </p>
                </figure>

                <figure className="figure-card">
                  <figcaption className="figure-caption">Split Ratio by Security Level</figcaption>
                  <svg viewBox="0 0 320 180" className="figure-svg" role="img" aria-label="Stacked horizontal bars showing quantum versus classical channel split for each security level">
                    {["low", "medium", "high", "critical"].map((level, i) => {
                      const { quantum, classical } = splitRatioFor(level)
                      const y = 30 + i * 34
                      const isCurrent = params.payloadSensitivity === level
                      const qw = quantum * 200
                      const cw = classical * 200
                      return (
                        <g key={level} opacity={isCurrent ? 1 : 0.5}>
                          <text x="40" y={y + 22} className="tick-label" style={{ fontSize: "9px", fontWeight: isCurrent ? 700 : 400 }}>
                            {level[0].toUpperCase() + level.slice(1)}
                          </text>
                          <rect x="95" y={y + 6} width={qw} height="18" rx="4" className="seg-secure" />
                          <rect x={95 + qw} y={y + 6} width={cw} height="18" rx="4" className="seg-abort" />
                          <text x={98 + qw} y={y + 19} className="bar-value" style={{ fontSize: "9px" }}>
                            {Math.round(quantum * 100)}% Q / {Math.round(classical * 100)}% C
                          </text>
                          {isCurrent && <rect x="90" y={y + 2} width="210" height="26" rx="6" className="current-ring" fill="none" strokeWidth="2" />}
                        </g>
                      )
                    })}
                    <rect x="95" y="164" width="10" height="10" className="seg-secure" />
                    <text x="109" y="173" className="tick-label">quantum channel</text>
                    <rect x="195" y="164" width="10" height="10" className="seg-abort" />
                    <text x="209" y="173" className="tick-label">classical channel</text>
                  </svg>
                  <p className="figure-explain">
                    How the hybrid system splits traffic across channels at each sensitivity
                    level. Low sensitivity leans classical (green bar shorter); critical
                    sensitivity forces 100% quantum with no classical fallback. The ring
                    highlights your current selection ({params.payloadSensitivity}): higher
                    sensitivity shifts the balance toward quantum at the cost of throughput.
                  </p>
                </figure>

                <figure className="figure-card">
                  <figcaption className="figure-caption">Throughput and Latency by Payload Size</figcaption>
                  <svg viewBox="0 0 320 180" className="figure-svg" role="img" aria-label="Dual line chart of throughput and transfer latency across payload sizes">
                    <line x1="40" y1="150" x2="300" y2="150" className="axis" />
                    <line x1="40" y1="10" x2="40" y2="150" className="axis" />
                    <polyline points={throughputCurve(params)} className="chart-line" fill="none" />
                    <polyline points={latencyCurve(params)} className="chart-line-alt" fill="none" />
                    <line x1={payloadToX(params.payloadSize)} y1="10" x2={payloadToX(params.payloadSize)} y2="150" className="marker-line" />
                    <circle cx={payloadToX(params.payloadSize)} cy={throughputToY(throughputFor(params.payloadSize, params))} r="5" className="marker-dot" />
                    <text x={Math.min(payloadToX(params.payloadSize) + 8, 240)} y="24" className="marker-label">
                      {Math.round(throughputFor(params.payloadSize, params))} MB/s
                    </text>
                    <rect x="40" y="160" width="10" height="10" className="seg-secure" />
                    <text x="54" y="169" className="tick-label">throughput (MB/s)</text>
                    <rect x="170" y="160" width="10" height="10" className="seg-abort" />
                    <text x="184" y="169" className="tick-label">transfer time (s)</text>
                    <text x="44" y="20" className="tick-label">max</text>
                    <text x="44" y="146" className="tick-label">0</text>
                    <text x="40" y="176" className="axis-label" textAnchor="start">Payload: 1 MB ... 10 GB</text>
                  </svg>
                  <p className="figure-explain">
                    Green line: effective throughput (payload divided by transfer time).
                    Red line: wall-clock transfer time. Small payloads are dominated by
                    fixed latency ({params.classicalLatency} ms x 3 round trips), so
                    throughput starts low then plateaus as payload grows. Your marker shows
                    the current {params.payloadSize} MB payload: {results.transferTime}s to transfer.
                  </p>
                </figure>
              </div>
            </section>
          </section>
        </div>
      </main>
    </div>
  )
}

function ParamControl({ param, value, onChange, isActive, onFocus, onBlur }) {
  const isRange = param.type === "range"
  const isSelect = param.type === "select"

  return (
    <div className={`param-control ${isActive ? "active" : ""}`}>
      <div className="param-label-row">
        <button
          type="button"
          className="param-info-toggle"
          onClick={(e) => {
            e.stopPropagation()
            onFocus()
          }}
          aria-label={`Explain ${param.label}`}
          aria-expanded={isActive}
        >
          ?
        </button>
        <label htmlFor={param.id} className="param-label" onClick={() => onFocus?.()}>
          <span className="param-name">{param.label}</span>
          <span className="param-value">{value}{param.unit && ` ${param.unit}`}</span>
        </label>
      </div>

      <div className="param-input-wrapper">
        {isRange && (
          <input
            id={param.id}
            type="range"
            min={param.min}
            max={param.max}
            step={param.step}
            value={value}
            onChange={(e) => onChange(param.id, Number(e.target.value))}
            onFocus={onFocus}
            onBlur={onBlur}
            className="param-slider"
            aria-label={param.label}
          />
        )}
        {isSelect && (
          <select
            id={param.id}
            value={value}
            onChange={(e) => onChange(param.id, e.target.value)}
            onFocus={onFocus}
            onBlur={onBlur}
            className="param-select"
            aria-label={param.label}
          >
            {param.options.map(opt => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        )}
      </div>

      {isActive && (
        <div className="param-explanation">
          <p className="explanation-text">{param.explanation}</p>
          <div className="explanation-affects">
            <strong>Affects:</strong> {param.affects.join(", ")}
          </div>
        </div>
      )}
    </div>
  )
}

function ResultCard({ result, value, params, index, animate }) {
  const interpretation = result.interpretation(value, params)
  const isWarning = interpretation.startsWith("⚠")
  const isGood = interpretation.startsWith("✓") || interpretation.startsWith("Excellent") || interpretation.startsWith("Good") || interpretation.startsWith("High") || interpretation.startsWith("Five nines") || interpretation.startsWith("Instantaneous") || interpretation.startsWith("Fast")

  return (
    <article
      className={`result-card ${isWarning ? "warning" : ""} ${isGood ? "good" : ""}`}
      style={{ animationDelay: `${index * 80}ms` }}
    >
      <header className="result-header">
        <h3 className="result-label">{result.label}</h3>
        <span className="result-value">{value}{result.unit && ` ${result.unit}`}</span>
      </header>
      <p className="result-description">{result.description}</p>
      <div className={`result-interpretation ${isWarning ? "warning" : ""} ${isGood ? "good" : ""}`}>
        {interpretation}
      </div>
    </article>
  )
}