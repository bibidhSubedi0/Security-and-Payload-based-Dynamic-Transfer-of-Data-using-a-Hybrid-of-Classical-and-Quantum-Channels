import { useState, useEffect } from "react"
import "./Home.css"

const QUANTUM_JARGONS = [
  { term: "Qubit", definition: "Quantum bit — the fundamental unit of quantum information, existing in superposition of |0⟩ and |1⟩" },
  { term: "Superposition", definition: "Ability of a quantum system to exist in multiple states simultaneously until measured" },
  { term: "Entanglement", definition: "Quantum correlation where particles become linked, and the state of one instantly determines the other" },
  { term: "Quantum Channel", definition: "Communication channel that transmits quantum states, enabling QKD and quantum teleportation" },
  { term: "Classical Channel", definition: "Traditional communication channel transmitting classical bits (0 or 1)" },
  { term: "QKD", definition: "Quantum Key Distribution — protocol using quantum mechanics to securely distribute encryption keys" },
  { term: "BB84 Protocol", definition: "First QKD protocol using photon polarization states for key exchange" },
  { term: "Decoherence", definition: "Loss of quantum coherence due to environmental interaction, causing quantum information loss" },
  { term: "Bell State", definition: "Maximally entangled two-qubit states forming basis for quantum teleportation" },
  { term: "No-Cloning Theorem", definition: "Impossible to create identical copy of arbitrary unknown quantum state" },
]

export default function Home() {
  const [darkMode, setDarkMode] = useState(() => {
    const saved = localStorage.getItem("darkMode")
    return saved ? JSON.parse(saved) : window.matchMedia("(prefers-color-scheme: dark)").matches
  })
  const [fontColor, setFontColor] = useState(() => localStorage.getItem("fontColor") || "#1a1a2e")
  const [animate, setAnimate] = useState(false)

  useEffect(() => {
    localStorage.setItem("darkMode", JSON.stringify(darkMode))
    document.documentElement.classList.toggle("dark", darkMode)
  }, [darkMode])

  useEffect(() => {
    localStorage.setItem("fontColor", fontColor)
    document.documentElement.style.setProperty("--font-color", fontColor)
  }, [fontColor])

  useEffect(() => {
    const timer = setTimeout(() => setAnimate(true), 100)
    return () => clearTimeout(timer)
  }, [])

  const lightFontColors = ["#1a1a2e", "#8b0000", "#dc143c", "#b22222", "#ff6b6b", "#e74c3c"]
  const darkFontColors = ["#f8f0ff", "#d8b4fe", "#c084fc", "#a855f7", "#9333ea", "#7e22ce"]

  return (
    <div className={`home ${darkMode ? "dark" : "light"} ${animate ? "animate-in" : ""}`}>
      <div className="particles" aria-hidden="true">
        {[...Array(30)].map((_, i) => (
          <span key={i} className="particle" style={{ "--i": i }} />
        ))}
      </div>

      <header className="header">
        <div className="logo-section">
          <span className="logo-icon">⚛</span>
          <h1 className="logo-text">QuantumTransfer</h1>
        </div>
        <div className="controls">
          <div className="font-picker">
            <label htmlFor="fontColor">Font: </label>
            <input
              id="fontColor"
              type="color"
              value={fontColor}
              onChange={(e) => setFontColor(e.target.value)}
              aria-label="Font color"
            />
          </div>
          <button
            className="theme-toggle"
            onClick={() => setDarkMode(!darkMode)}
            aria-label={darkMode ? "Switch to light mode" : "Switch to dark mode"}
          >
            {darkMode ? "☀" : "🌙"}
          </button>
        </div>
      </header>

      <main className="main">
        <section className="hero">
          <div className="hero-content">
            <h2 className="hero-title">Security & Payload-based Dynamic Transfer</h2>
            <p className="hero-subtitle">
              Using a Hybrid of Classical and Quantum Channels
            </p>
            <p className="hero-description">
              A novel approach to secure data transmission that leverages quantum key distribution
              for unconditional security on the quantum channel, while utilizing classical channels
              for high-throughput payload delivery. The system dynamically adapts transfer strategies
              based on real-time channel conditions, security requirements, and payload sensitivity.
            </p>
            <div className="hero-stats">
              <div className="stat">
                <span className="stat-value">2</span>
                <span className="stat-label">Channels</span>
              </div>
              <div className="stat">
                <span className="stat-value">∞</span>
                <span className="stat-label">Security</span>
              </div>
              <div className="stat">
                <span className="stat-value">Dynamic</span>
                <span className="stat-label">Adaptation</span>
              </div>
            </div>
          </div>
        </section>

        <section className="jargons-section">
          <h3 className="section-title">Quantum Lexicon</h3>
          <p className="section-subtitle">Essential terminology for understanding quantum communication</p>
          <div className="jargons-grid">
            {QUANTUM_JARGONS.map(({ term, definition }, idx) => (
              <article key={term} className="jargon-card" style={{ "--delay": `${idx * 100}ms` }}>
                <h4 className="jargon-term">{term}</h4>
                <p className="jargon-definition">{definition}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="architecture-section">
          <h3 className="section-title">Hybrid Architecture</h3>
          <div className="architecture-diagram">
            <div className="channel quantum-channel">
              <span className="channel-icon">⚛</span>
              <span className="channel-name">Quantum Channel</span>
              <ul className="channel-features">
                <li>QKD Key Exchange</li>
                <li>Entanglement Distribution</li>
                <li>Eavesdropping Detection</li>
              </ul>
            </div>
            <div className="bridge">⇄</div>
            <div className="channel classical-channel">
              <span className="channel-icon">📡</span>
              <span className="channel-name">Classical Channel</span>
              <ul className="channel-features">
                <li>Encrypted Payload Transfer</li>
                <li>Authentication & Sync</li>
                <li>Error Correction</li>
              </ul>
            </div>
          </div>
        </section>
      </main>

      <footer className="footer">
        <p>QuantumTransfer — Securing the future, one qubit at a time</p>
      </footer>
    </div>
  )
}