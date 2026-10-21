import { useState, useEffect } from "react"
import "./Home.css"

const QUANTUM_JARGONS = [
  {
    term: "Qubit",
    definition: "Quantum bit - the fundamental unit of quantum information, existing in superposition of |0> and |1>",
    url: "https://en.wikipedia.org/wiki/Qubit"
  },
  {
    term: "Superposition",
    definition: "Ability of a quantum system to exist in multiple states simultaneously until measured",
    url: "https://en.wikipedia.org/wiki/Quantum_superposition"
  },
  {
    term: "Entanglement",
    definition: "Quantum correlation where particles become linked, and the state of one instantly determines the other",
    url: "https://en.wikipedia.org/wiki/Quantum_entanglement"
  },
  {
    term: "Quantum Channel",
    definition: "Communication channel that transmits quantum states, enabling QKD and quantum teleportation",
    url: "https://en.wikipedia.org/wiki/Quantum_channel"
  },
  {
    term: "Classical Channel",
    definition: "Traditional communication channel transmitting classical bits (0 or 1)",
    url: "https://www.sciencedirect.com/topics/mathematics/classical-channel"
  },
  {
    term: "QKD",
    definition: "Quantum Key Distribution - protocol using quantum mechanics to securely distribute encryption keys",
    url: "https://en.wikipedia.org/wiki/Quantum_key_distribution"
  },
  {
    term: "BB84 Protocol",
    definition: "First QKD protocol using photon polarization states for key exchange",
    url: "https://en.wikipedia.org/wiki/BB84"
  },
  {
    term: "Decoherence",
    definition: "Loss of quantum coherence due to environmental interaction, causing quantum information loss",
    url: "https://en.wikipedia.org/wiki/Quantum_decoherence"
  },
  {
    term: "Bell State",
    definition: "Maximally entangled two-qubit states forming basis for quantum teleportation",
    url: "https://en.wikipedia.org/wiki/Bell_state"
  },
  {
    term: "No-Cloning Theorem",
    definition: "Impossible to create identical copy of arbitrary unknown quantum state",
    url: "https://en.wikipedia.org/wiki/No-cloning_theorem"
  }
]

export default function Home() {
  const [darkMode, setDarkMode] = useState(() => {
    const saved = localStorage.getItem("darkMode")
    return saved ? JSON.parse(saved) : window.matchMedia("(prefers-color-scheme: dark)").matches
  })
  const [animate, setAnimate] = useState(false)

  useEffect(() => {
    localStorage.setItem("darkMode", JSON.stringify(darkMode))
    document.documentElement.classList.toggle("dark", darkMode)
  }, [darkMode])

  useEffect(() => {
    const timer = setTimeout(() => setAnimate(true), 100)
    return () => clearTimeout(timer)
  }, [])

  return (
    <div className={`home ${darkMode ? "dark" : "light"} ${animate ? "animate-in" : ""}`}>
      <div className="particles" aria-hidden="true">
        {[...Array(25)].map((_, i) => (
          <span
            key={i}
            className="petal"
            style={{
              "--i": i,
              "--delay": `${i * 150}ms`,
              "--duration": `${12 + Math.random() * 8}s`,
              "--size": `${8 + Math.random() * 12}px`,
              "--drift": `${(Math.random() - 0.5) * 100}px`,
              "--rotate": `${Math.random() * 360}deg`,
              "--sway": `${(Math.random() - 0.5) * 60}px`,
            }}
          />
        ))}
      </div>

      <header className="header">
        <div className="logo-section">
          <span className="logo-icon">⚛</span>
          <h1 className="logo-text">QuantumTransfer</h1>
        </div>
        <button
          className="theme-toggle"
          onClick={() => setDarkMode(!darkMode)}
          aria-label={darkMode ? "Switch to light mode" : "Switch to dark mode"}
        >
          {darkMode ? "☀" : "🌙"}
        </button>
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
          <p className="section-subtitle">Essential terminology for understanding quantum communication - click any term for IBM Quantum Learning</p>
          <div className="jargons-grid">
            {QUANTUM_JARGONS.map(({ term, definition, url }, idx) => (
              <article key={term} className="jargon-card" style={{ "--delay": `${idx * 100}ms` }}>
                <div className="jargon-header">
                  <a
                    href={url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="jargon-link"
                    aria-label={`Learn more about ${term} on IBM Quantum Learning`}
                  >
                    <h4 className="jargon-term">{term}</h4>
                    <span className="external-icon" aria-hidden="true">↗</span>
                  </a>
                </div>
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
        <p>QuantumTransfer - 2026</p>
      </footer>
    </div>
  )
}