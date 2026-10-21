import { useState, useEffect } from "react"
import Home from "./pages/Home"
import Test from "./pages/Test"
import "./App.css"

function App() {
  const [page, setPage] = useState(() => {
    const hash = window.location.hash.slice(1)
    return hash === "test" ? "test" : "home"
  })

  useEffect(() => {
    const onHashChange = () => {
      const hash = window.location.hash.slice(1)
      setPage(hash === "test" ? "test" : "home")
    }
    window.addEventListener("hashchange", onHashChange)
    return () => window.removeEventListener("hashchange", onHashChange)
  }, [])

  return page === "test" ? <Test /> : <Home />
}

export default App