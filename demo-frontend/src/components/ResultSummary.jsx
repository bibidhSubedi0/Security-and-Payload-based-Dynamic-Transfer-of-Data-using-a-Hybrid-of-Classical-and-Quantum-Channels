const PASS = new Set(["CLEAN_PASS", "RECOVERED_VIA_REROUTE"]);
const ABORT = new Set(["SESSION_ABORTED", "RECON_INCOMPLETE"]);

export default function ResultSummary({ result }) {
  if (!result) return null;

  const isPass = PASS.has(result.outcome);
  const isAbort = ABORT.has(result.outcome);
  const bannerClass = isPass ? "banner pass" : isAbort ? "banner abort" : "banner error";

  const rows = [
    ["QBER (measured)", fmt(result.qber, v => `${(v * 100).toFixed(2)} %`)],
    ["CHSH S", result.chsh != null && result.protocol === "E91"
      ? `${result.chsh.toFixed(3)} ${Math.abs(result.chsh) > 2 ? "(quantum ✓)" : "(classical!)"}`
      : null],
    ["Split (quantum / classical)", result.quantum_fraction != null
      ? `${Math.round(result.quantum_fraction * 100)}% / ${Math.round(result.classical_fraction * 100)}%`
      : null],
    ["Split reason", result.split_reason],
    ["Secret key rate", fmt(result.skr, v => `${v.toFixed(1)} bits/s`)],
    ["Throughput", fmt(result.throughput_bps, v => `${v.toFixed(1)} B/s`)],
    ["Latency", fmt(result.latency_s, v => `${(v * 1000).toFixed(1)} ms`)],
    ["Abort reason", result.abort_reason],
  ].filter(([, v]) => v !== null && v !== undefined);

  return (
    <section className="panel">
      <div className={bannerClass}>
        {isPass && `✓ ${result.outcome}`}
        {isAbort && `✗ ${result.outcome.replace("_", " ")}`}
        {!isPass && !isAbort && `⚠ ${result.outcome}`}
      </div>
      {rows.length > 0 && (
        <table className="results">
          <tbody>
            {rows.map(([k, v]) => (
              <tr key={k}>
                <td>{k}</td>
                <td><strong>{v}</strong></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function fmt(value, f) {
  return value == null ? null : f(value);
}
