export default function ControlPanel({
  protocol, setProtocol,
  securityLevel, setSecurityLevel,
  noise, setNoise,
  payloadText, setPayloadText,
  isRunning, onRun,
}) {
  const pct = Math.min(100, (noise / 0.15) * 100);

  return (
    <section className="panel">
      <div className="row">
        <label>
          Protocol
          <select value={protocol} onChange={e => setProtocol(e.target.value)} disabled={isRunning}>
            <option value="BB84">BB84</option>
            <option value="E91">E91</option>
          </select>
        </label>

        <label>
          Security level
          <select value={securityLevel} onChange={e => setSecurityLevel(e.target.value)} disabled={isRunning}>
            <option value="low">low (25% quantum)</option>
            <option value="medium">medium (50% quantum)</option>
            <option value="high">high (75% quantum)</option>
          </select>
        </label>

        <label className="grow">
          Payload
          <input
            type="text"
            value={payloadText}
            onChange={e => setPayloadText(e.target.value)}
            disabled={isRunning}
          />
        </label>
      </div>

      <div className="row slider-row">
        <label className="grow">
          Channel noise (bit-flip probability): <strong>{noise.toFixed(2)}</strong>
          <input
            type="range"
            min="0"
            max="0.15"
            step="0.01"
            value={noise}
            onChange={e => setNoise(parseFloat(e.target.value))}
            disabled={isRunning}
          />
          <div className="slider-scale">
            <div className="marker" style={{ left: `${(0.11 / 0.15) * 100}%` }} />
            <span>abort threshold 0.11</span>
          </div>
        </label>
      </div>

      {noise >= 0.10 && noise <= 0.12 && (
        <p className="warning">
          Near the QBER abort threshold — outcome is probabilistic here.
          Above ~0.13 the session reliably refuses to derive a key.
        </p>
      )}

      <button className="run-btn" onClick={onRun} disabled={isRunning}>
        {isRunning ? "Running transfer…" : "Run Transfer"}
      </button>
    </section>
  );
}
