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
          <a href="/" className="nav-brand">
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
              {RESULTS.map((result, idx) => (
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

            <div className="legend">
              <h4>Legend</h4>
              <div className="legend-items">
                <span className="legend-item good">✓ Excellent / Good</span>
                <span className="legend-item warn">⚠ Warning / Attention</span>
                <span className="legend-item info">ℹ Informational</span>
              </div>
            </div>
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
      <label htmlFor={param.id} className="param-label" onClick={() => onFocus?.()}>
        <span className="param-name">{param.label}</span>
        <span className="param-value">{value}{param.unit && ` ${param.unit}`}</span>
      </label>

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