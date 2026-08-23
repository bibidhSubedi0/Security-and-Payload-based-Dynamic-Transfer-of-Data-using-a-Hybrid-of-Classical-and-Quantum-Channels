import { useState } from "react";
import ControlPanel from "./components/ControlPanel";
import LogTerminal from "./components/LogTerminal";
import ResultSummary from "./components/ResultSummary";

const API = "http://localhost:8000";
const WS = "ws://localhost:8000/ws/run-session";

export default function App() {
  const [protocol, setProtocol] = useState("BB84");
  const [securityLevel, setSecurityLevel] = useState("medium");
  const [noise, setNoise] = useState(0);
  const [payloadText, setPayloadText] = useState("hello-panel-demo");
  const [isRunning, setIsRunning] = useState(false);
  const [logLines, setLogLines] = useState([]);
  const [result, setResult] = useState(null);
  const [transport, setTransport] = useState(null);

  async function handleRun() {
    setIsRunning(true);
    setResult(null);
    setLogLines([]);
    try {
      await runOverWebSocket();
      setTransport("websocket");
    } catch {
      setTransport("rest");
      await runOverRest();
    } finally {
      setIsRunning(false);
    }
  }

  function runOverWebSocket() {
    return new Promise((resolve, reject) => {
      let ws;
      let settled = false;
      const failGuard = setTimeout(() => {
        if (!settled) {
          settled = true;
          try { ws && ws.close(); } catch {}
          reject(new Error("ws timeout"));
        }
      }, 3000);

      try {
        ws = new WebSocket(WS);
      } catch {
        clearTimeout(failGuard);
        reject(new Error("ws unsupported"));
        return;
      }

      ws.onopen = () => {
        clearTimeout(failGuard);
        ws.send(JSON.stringify({
          protocol,
          security_level: securityLevel,
          noise,
          payload_text: payloadText,
        }));
      };
      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === "log") {
          setLogLines(prev => [...prev, msg.line]);
        } else if (msg.type === "result") {
          setResult(msg.data);
          settled = true;
          resolve();
        }
      };
      ws.onerror = () => {
        if (!settled) {
          settled = true;
          reject(new Error("ws error"));
        }
      };
      ws.onclose = () => {
        if (!settled) {
          settled = true;
          reject(new Error("ws closed early"));
        }
      };
    });
  }

  async function runOverRest() {
    const res = await fetch(`${API}/run-session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        protocol,
        security_level: securityLevel,
        noise,
        payload_text: payloadText,
      }),
    });
    if (!res.ok) throw new Error(`API ${res.status}`);
    const data = await res.json();
    setLogLines(data.log_lines || []);
    setResult(data);
  }

  return (
    <div className="app">
      <header>
        <h1>Hybrid Quantum-Classical Transfer</h1>
        <p className="subtitle">
          QKD key exchange &middot; dynamic payload split &middot; live channel telemetry
        </p>
      </header>

      <ControlPanel
        protocol={protocol} setProtocol={setProtocol}
        securityLevel={securityLevel} setSecurityLevel={setSecurityLevel}
        noise={noise} setNoise={setNoise}
        payloadText={payloadText} setPayloadText={setPayloadText}
        isRunning={isRunning}
        onRun={handleRun}
      />

      <ResultSummary result={result} />

      <LogTerminal logLines={logLines} isRunning={isRunning} transport={transport} />
    </div>
  );
}
