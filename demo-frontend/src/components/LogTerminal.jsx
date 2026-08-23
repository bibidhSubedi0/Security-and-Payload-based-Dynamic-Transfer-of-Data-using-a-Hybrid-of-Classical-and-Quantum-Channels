import { useEffect, useRef } from "react";

export default function LogTerminal({ logLines, isRunning, transport }) {
  const boxRef = useRef(null);

  useEffect(() => {
    if (boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [logLines]);

  return (
    <section className="panel">
      <div className="panel-title">
        Live log {transport && <span className={`badge ${transport}`}>{transport}</span>}
        {isRunning && <span className="pulse"> ●</span>}
      </div>
      <div className="terminal" ref={boxRef}>
        {logLines.length === 0 && !isRunning && (
          <div className="dim">— no session run yet —</div>
        )}
        {logLines.map((line, i) => (
          <div key={i} className={lineClass(line)}>{line}</div>
        ))}
      </div>
    </section>
  );
}

function lineClass(line) {
  if (line.includes("CRITICAL") || line.includes("SECURITY")) return "log-critical";
  if (line.includes('"levelname": "WARNING"')) return "log-warn";
  if (line.includes("Session aborted") || line.includes("aborted")) return "log-error";
  return "";
}
