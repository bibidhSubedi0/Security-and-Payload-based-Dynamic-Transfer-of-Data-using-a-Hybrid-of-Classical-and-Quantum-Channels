"""
Node — Phase 2

A single Node class covers both the Alice (sender/initiator) and Bob (receiver/acceptor)
roles. Role is determined by which method is called first:
  - request_connection()   → marks this node as Alice
  - wait_for_connection()  → marks this node as Bob

Both roles call run_qkd() afterward. Alice sends REQUEST_EBITS to trigger the server;
Bob just blocks waiting for the server-pushed EBIT_RESULT.

If the QKD session is aborted (QBER > threshold after retries, or CHSH ≤ 2 for E91),
run_qkd() raises SessionAbortedError with the reason and measured values.
"""

import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from metrics.logger import get_logger

logger = get_logger("node")


class SessionAbortedError(Exception):
    def __init__(self, reason: str, qber: float, chsh: float | None = None) -> None:
        self.reason = reason
        self.qber = qber
        self.chsh = chsh
        super().__init__(
            f"QKD session aborted: {reason} "
            f"(QBER={qber:.4f}"
            + (f", CHSH={chsh:.4f}" if chsh is not None else "")
            + ")"
        )


@dataclass
class QKDSession:
    role: str           # "alice" | "bob"
    key: list[int]
    qber: float
    chsh: float | None
    attempt: int        # which attempt succeeded (1 or 2)


class Node:
    def __init__(self, node_id: str, config: dict[str, Any]) -> None:
        self.node_id = node_id
        self.config = config
        self._sock: socket.socket | None = None
        self._reader = None
        self._is_alice: bool | None = None  # set after connect/wait_for_connection
        self.session: QKDSession | None = None
        # Phase 5 — dual-channel transmission
        self.last_received_payload: bytes | None = None
        self._last_tx_timings: dict = {}   # populated by transmit_payload()
        self._last_rx_timings: dict = {}   # populated by receive_payload()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((self.config["host"], self.config["server_port"]))
        self._reader = self._sock.makefile("r")
        logger.info("Connected to server", extra={
            "node_id": self.node_id,
            "server": f"{self.config['host']}:{self.config['server_port']}",
        })

    def close(self) -> None:
        if self._reader:
            try:
                self._reader.close()
            except OSError:
                pass
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Low-level send / recv
    # ------------------------------------------------------------------

    def _send(self, msg: dict) -> None:
        data = (json.dumps(msg) + "\n").encode()
        self._sock.sendall(data)

    def _recv(self) -> dict:
        line = self._reader.readline()
        if not line:
            raise ConnectionError(
                f"[{self.node_id}] Server closed connection unexpectedly"
            )
        return json.loads(line.strip())

    # ------------------------------------------------------------------
    # Registry / discovery
    # ------------------------------------------------------------------

    def register(self) -> None:
        self._send({"type": "REGISTER", "node_id": self.node_id})
        resp = self._recv()
        if resp["type"] != "REGISTERED":
            raise RuntimeError(f"[{self.node_id}] Registration failed: {resp}")
        logger.info("Registered", extra={"node_id": self.node_id})

    def list_nodes(self) -> list[str]:
        self._send({"type": "LIST_NODES"})
        resp = self._recv()
        return resp["nodes"]

    def wait_for_peer(self, peer_id: str, timeout: float = 10.0) -> bool:
        """Poll server until peer_id appears in the registry, or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if peer_id in self.list_nodes():
                return True
            time.sleep(0.05)
        return False

    # ------------------------------------------------------------------
    # Connection handshake
    # ------------------------------------------------------------------

    def request_connection(self, peer_id: str) -> None:
        """Alice: ask server to connect to peer. Blocks until accepted/rejected."""
        self._send({"type": "CONNECT_REQUEST", "to": peer_id})
        resp = self._recv()
        if resp["type"] != "CONNECT_ACCEPTED":
            raise ConnectionRefusedError(
                f"[{self.node_id}] Connection to {peer_id} rejected: {resp}"
            )
        self._is_alice = True
        logger.info("Connection accepted", extra={
            "node_id": self.node_id, "peer": peer_id
        })

    def wait_for_connection(self) -> str:
        """Bob: block until server forwards a NOTIFY_CONNECT, then accept."""
        msg = self._recv()
        if msg["type"] != "NOTIFY_CONNECT":
            raise RuntimeError(
                f"[{self.node_id}] Expected NOTIFY_CONNECT, got {msg['type']}"
            )
        peer = msg["from_node"]
        self._send({"type": "ACCEPT", "from_node": peer})

        # Wait for server confirmation that the session is live
        confirm = self._recv()
        if confirm["type"] != "CONNECT_ESTABLISHED":
            raise RuntimeError(
                f"[{self.node_id}] Expected CONNECT_ESTABLISHED, got {confirm['type']}"
            )
        self._is_alice = False
        logger.info("Connection established (Bob side)", extra={
            "node_id": self.node_id, "peer": peer
        })
        return peer

    # ------------------------------------------------------------------
    # QKD
    # ------------------------------------------------------------------

    def run_qkd(self, injected_noise: float | None = None) -> QKDSession:
        """
        Drive or receive the QKD phase.

        Alice sends REQUEST_EBITS to trigger the server.
        Bob just waits — the server pushes EBIT_RESULT after Alice's request.

        Both block until they receive EBIT_RESULT (success) or SESSION_ABORTED.
        On abort, raises SessionAbortedError.
        """
        if self._is_alice is None:
            raise RuntimeError(
                "call request_connection() or wait_for_connection() before run_qkd()"
            )

        noise = (
            injected_noise
            if injected_noise is not None
            else self.config["injected_qber"]
        )

        if self._is_alice:
            self._send({
                "type": "REQUEST_EBITS",
                "protocol": self.config["protocol"],
                "injected_noise": noise,
                "n_qubits": self.config.get("n_qubits", 200),
            })

        # Both Alice and Bob block here waiting for server response
        msg = self._recv()

        if msg["type"] == "EBIT_RESULT":
            self.session = QKDSession(
                role=msg["role"],
                key=msg["key"],
                qber=msg["qber"],
                chsh=msg.get("chsh"),
                attempt=msg.get("attempt", 1),
            )
            logger.info("QKD complete", extra={
                "node_id": self.node_id,
                "role": self.session.role,
                "key_len": len(self.session.key),
                "qber": self.session.qber,
                "chsh": self.session.chsh,
                "attempt": self.session.attempt,
            })
            return self.session

        if msg["type"] == "SESSION_ABORTED":
            raise SessionAbortedError(
                reason=msg["reason"],
                qber=msg["qber"],
                chsh=msg.get("chsh"),
            )

        raise RuntimeError(f"[{self.node_id}] Unexpected message in QKD phase: {msg}")

    # ------------------------------------------------------------------
    # Phase 5 — Dual-channel transmission
    # ------------------------------------------------------------------
    # Why threads (not asyncio): all networking code in this project is blocking
    # and thread-based. Qiskit's AerSimulator uses compiled C extensions that
    # release the GIL, giving genuine parallelism for the quantum work. Introducing
    # asyncio would require running the event loop in a thread anyway, adding
    # complexity with no benefit.
    # ------------------------------------------------------------------

    def transmit_payload(
        self,
        payload: bytes,
        decision: Any,                  # SplitDecision from Phase 4
        classical_dc: Any,              # DataChannel (classical, Phase 3)
        quantum_dc: Any,                # DataChannel (quantum, Phase 5)
        aes_key: bytes,
        available_ebits: int,
    ) -> None:
        """
        Alice: split payload, send both segments concurrently.

        Step 1 (sequential): send metadata JSON as the first frame on the classical
                              channel so Bob knows the byte boundary before data arrives.
        Step 2 (concurrent): quantum thread → SDC-encode and send over quantum_dc;
                              classical thread → AES-encrypt and send over classical_dc.

        Timing is recorded in self._last_tx_timings for the concurrency test.
        Any exception from either thread is re-raised after both join.
        """
        from transmission.payload_splitter import split_payload, make_metadata
        from transmission.quantum_transmit import encode_bytes_sdc
        from transmission.classical_transmit import send_classical_segment

        split = split_payload(payload, decision)
        meta = make_metadata(split)

        # Step 1 — metadata (negligible size; sent before concurrent phase)
        classical_dc.send(json.dumps(meta).encode())

        # Step 2 — concurrent send
        errors: dict[str, Exception] = {}
        timings: dict[str, float] = {}

        def _send_quantum() -> None:
            timings["q_start"] = time.monotonic()
            try:
                encoded = encode_bytes_sdc(split.quantum_segment, available_ebits)
                quantum_dc.send(encoded)
            except Exception as exc:
                errors["quantum"] = exc
            finally:
                timings["q_end"] = time.monotonic()

        def _send_classical() -> None:
            timings["c_start"] = time.monotonic()
            try:
                send_classical_segment(split.classical_segment, aes_key, classical_dc)
            except Exception as exc:
                errors["classical"] = exc
            finally:
                timings["c_end"] = time.monotonic()

        tq = threading.Thread(target=_send_quantum,   name=f"{self.node_id}-tx-quantum")
        tc = threading.Thread(target=_send_classical, name=f"{self.node_id}-tx-classical")
        tq.start()
        tc.start()
        tq.join()
        tc.join()

        self._last_tx_timings = timings
        logger.info("Payload transmitted", extra={
            "node_id": self.node_id,
            "total_len": meta["total_len"],
            "quantum_len": meta["quantum_len"],
            "classical_len": meta["classical_len"],
            "q_duration_s": round(timings.get("q_end", 0) - timings.get("q_start", 0), 4),
            "c_duration_s": round(timings.get("c_end", 0) - timings.get("c_start", 0), 4),
        })

        if "quantum" in errors:
            raise errors["quantum"]
        if "classical" in errors:
            raise errors["classical"]

    def receive_payload(
        self,
        classical_dc: Any,   # DataChannel
        quantum_dc: Any,     # DataChannel
        aes_key: bytes,
    ) -> bytes:
        """
        Bob: receive both segments concurrently, reassemble, store result.

        Step 1 (sequential): read metadata JSON from classical_dc (first frame).
        Step 2 (concurrent): quantum thread ← recv from quantum_dc;
                              classical thread ← recv + AES-decrypt from classical_dc.
        Step 3: reassemble (quantum_segment + classical_segment) and validate lengths.

        Result is stored in self.last_received_payload for Phase 6 echo comparison.
        Timing is recorded in self._last_rx_timings.
        """
        from transmission.payload_splitter import reassemble_from_segments
        from transmission.classical_transmit import recv_classical_segment

        # Step 1 — metadata
        meta = json.loads(classical_dc.recv().decode())

        # Step 2 — concurrent receive
        received: dict[str, bytes] = {}
        errors: dict[str, Exception] = {}
        timings: dict[str, float] = {}

        def _recv_quantum() -> None:
            timings["q_start"] = time.monotonic()
            try:
                received["quantum"] = quantum_dc.recv()
            except Exception as exc:
                errors["quantum"] = exc
            finally:
                timings["q_end"] = time.monotonic()

        def _recv_classical() -> None:
            timings["c_start"] = time.monotonic()
            try:
                received["classical"] = recv_classical_segment(classical_dc, aes_key)
            except Exception as exc:
                errors["classical"] = exc
            finally:
                timings["c_end"] = time.monotonic()

        tq = threading.Thread(target=_recv_quantum,   name=f"{self.node_id}-rx-quantum")
        tc = threading.Thread(target=_recv_classical, name=f"{self.node_id}-rx-classical")
        tq.start()
        tc.start()
        tq.join()
        tc.join()

        self._last_rx_timings = timings

        if errors:
            raise next(iter(errors.values()))

        # Step 3 — reassemble and validate
        self.last_received_payload = reassemble_from_segments(
            received["quantum"], received["classical"], meta
        )
        logger.info("Payload received and reassembled", extra={
            "node_id": self.node_id,
            "total_len": meta["total_len"],
            "quantum_len": meta["quantum_len"],
            "classical_len": meta["classical_len"],
        })
        return self.last_received_payload
