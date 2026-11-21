"""
FastAPI backend for the live quantum-classical transfer demo.

Run:
    .venv/bin/uvicorn demo.backend.main:app --port 8000

Config key names verified against quantum_demo/pipeline.py::_normalize_config():
    protocol, security_level, injected_qber, payload_size_bytes.
The payload text travels as the `payload=` parameter (bytes), NOT a config key.
"""

import sys
import pathlib

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from demo.backend.schemas import RunSessionRequest, RunSessionResponse
from quantum_demo.pipeline import run_session
from quantum_demo.models import SessionResult

app = FastAPI(title="Hybrid Quantum-Classical Transfer Demo")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _to_response(result: SessionResult) -> RunSessionResponse:
    return RunSessionResponse(
        outcome=result.outcome,
        protocol=result.protocol,
        qber=result.qber,
        chsh=result.chsh,
        skr=result.skr,
        quantum_fraction=result.split.quantum_fraction if result.split else None,
        classical_fraction=result.split.classical_fraction if result.split else None,
        split_reason=result.split.reason if result.split else None,
        throughput_bps=result.throughput_bps,
        latency_s=result.latency_s,
        abort_reason=result.abort_reason,
        log_lines=result.log_lines,
    )


@app.post("/run-session", response_model=RunSessionResponse)
def run_session_endpoint(req: RunSessionRequest) -> RunSessionResponse:
    config = {
        "protocol": req.protocol,
        "security_level": req.security_level,
        "injected_qber": req.noise,
        "payload_size_bytes": len(req.payload_text.encode()),
    }
    result = run_session(config, payload=req.payload_text.encode())
    return _to_response(result)


@app.websocket("/ws/run-session")
async def ws_run_session(websocket: WebSocket) -> None:
    """
    Streams each structured log line AS IT IS EMITTED, then the final result.

    Message protocol (JSON):
      client -> {"protocol", "security_level", "noise", "payload"}
      server <- {"type": "log", "line": str}     (zero or more)
      server <- {"type": "result", "data": {...}}
    """
    import asyncio
    import queue

    await websocket.accept()
    try:
        req = RunSessionRequest(**(await websocket.receive_json()))
        config = {
            "protocol": req.protocol,
            "security_level": req.security_level,
            "injected_qber": req.noise,
            "payload_size_bytes": len(req.payload_text.encode()),
        }

        # run_session() blocks (threads + AerSimulator) and its on_log_line
        # callback fires on pipeline worker threads. Bridge the two worlds:
        # callback -> thread-safe queue; async drain task -> websocket.
        line_q: queue.Queue = queue.Queue()

        def on_log_line(line: str) -> None:
            line_q.put(line)

        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        result_holder: list = []

        def _blocking() -> None:
            try:
                result_holder.append(
                    run_session(config, payload=req.payload_text.encode(),
                                on_log_line=on_log_line)
                )
            finally:
                line_q.put(None)  # sentinel
                loop.call_soon_threadsafe(done.set)

        runner = asyncio.create_task(asyncio.to_thread(_blocking))

        async def _drain() -> None:
            while True:
                try:
                    line = await loop.run_in_executor(None, line_q.get)
                except Exception:
                    return
                if line is None:
                    return
                await websocket.send_json({"type": "log", "line": line})

        drain = asyncio.create_task(_drain())
        await done.wait()
        await drain
        result = result_holder[0]
        await websocket.send_json({"type": "result",
                                   "data": _to_response(result).model_dump()})
    except WebSocketDisconnect:
        return


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
