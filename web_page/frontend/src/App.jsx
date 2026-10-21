import { useState, useEffect } from "react"
import Home from "./pages/Home"
import Test from "./pages/Test"
import "./App.css"

function getRoute() {
  const hash = window.location.hash.slice(1)
  return hash.replace(/^\/+/, "") === "test" ? "test" : "home"
}

function App() {
  const [page, setPage] = useState(getRoute)

  useEffect(() => {
    const onHashChange = () => setPage(getRoute())
    window.addEventListener("hashchange", onHashChange)
    return () => window.removeEventListener("hashchange", onHashChange)
  }, [])

  return page === "test" ? <Test /> : <Home />
}

export default App