import { useEffect, useState } from "react"

function App() {
  const [status, setStatus] = useState("loading")

  useEffect(() => {
    fetch("/api/health/")
      .then((r) => r.json())
      .then((data) => setStatus(JSON.stringify(data, null, 2)))
      .catch((e) => setStatus(`Error: ${e.message}`))
  }, [])

  return (
    <div style={{ padding: "2rem", fontFamily: "system-ui" }}>
      <h1>Quantum Transfer Frontend</h1>
      <pre>{status}</pre>
    </div>
  )
}

export default App