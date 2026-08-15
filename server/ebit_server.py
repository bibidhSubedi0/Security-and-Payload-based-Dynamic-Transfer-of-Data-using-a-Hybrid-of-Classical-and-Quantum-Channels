"""
Ebit Server — Phase 2

Responsibilities:
  - Node registry (name → socket)
  - Connection request validation between nodes
  - QKD simulation (BB84 or E91) and ebit-result distribution to both nodes
  - Retry logic: up to MAX_RETRIES attempts before sending SESSION_ABORTED

One handler thread per connected node. Per-socket write locks allow any thread
to safely push messages to any node.
"""

import json
import socket
import threading
from typing import Any

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from quantum.bb84 import run_bb84, BB84_QBER_THRESHOLD, BB84_CHECK_FRACTION
from quantum.e91 import run_e91
from metrics.logger import get_logger

logger = get_logger("ebit_server")

MAX_RETRIES = 2


class EbitServer:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._host: str = config["host"]
        self._port: int = config["server_port"]

        # node_id → {"conn": socket, "lock": Lock}
        self._registry: dict[str, dict] = {}
        # node_id → peer_id (both directions stored after connection)
        self._sessions: dict[str, str] = {}
        # (from_id, to_id) → {"event": Event, "result": str|None}
        self._pending: dict[tuple[str, str], dict] = {}

        self._registry_lock = threading.Lock()
        self._server_sock: socket.socket | None = None
        self._running = False
        self.ready = threading.Event()   # set once accept loop is running

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self._host, self._port))
        self._server_sock.listen(5)
        self._running = True
        t = threading.Thread(target=self._accept_loop, daemon=True, name="server-accept")
        t.start()
        self.ready.set()
        logger.info("Ebit server started", extra={"host": self._host, "port": self._port})

    def stop(self) -> None:
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, addr = self._server_sock.accept()
            except OSError:
                break
            logger.info("New connection", extra={"addr": str(addr)})
            t = threading.Thread(
                target=self._handle_client, args=(conn,), daemon=True
            )
            t.start()

    def _send_raw(
        self, conn: socket.socket, lock: threading.Lock, msg: dict
    ) -> None:
        data = (json.dumps(msg) + "\n").encode()
        with lock:
            conn.sendall(data)

    def _send_to(self, node_id: str, msg: dict) -> None:
        with self._registry_lock:
            entry = self._registry.get(node_id)
        if entry:
            self._send_raw(entry["conn"], entry["lock"], msg)

    # ------------------------------------------------------------------
    # Per-connection handler
    # ------------------------------------------------------------------

    def _handle_client(self, conn: socket.socket) -> None:
        reader = conn.makefile("r")
        node_id: str | None = None
        write_lock = threading.Lock()

        try:
            while self._running:
                line = reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                mtype: str = msg.get("type", "")

                if mtype == "REGISTER":
                    node_id = msg["node_id"]
                    with self._registry_lock:
                        self._registry[node_id] = {"conn": conn, "lock": write_lock}
                    self._send_raw(conn, write_lock, {"type": "REGISTERED"})
                    logger.info("Node registered", extra={"node_id": node_id})

                elif mtype == "LIST_NODES":
                    with self._registry_lock:
                        nodes = [n for n in self._registry if n != node_id]
                    self._send_raw(conn, write_lock, {"type": "NODES", "nodes": nodes})

                elif mtype == "CONNECT_REQUEST":
                    peer_id: str = msg["to"]
                    with self._registry_lock:
                        peer_exists = peer_id in self._registry
                    if not peer_exists:
                        self._send_raw(conn, write_lock, {
                            "type": "CONNECT_REJECTED",
                            "reason": "peer_not_found",
                        })
                        continue

                    key = (node_id, peer_id)
                    event = threading.Event()
                    with self._registry_lock:
                        self._pending[key] = {"event": event, "result": None}

                    self._send_to(peer_id, {
                        "type": "NOTIFY_CONNECT", "from_node": node_id
                    })

                    accepted = event.wait(timeout=30)
                    with self._registry_lock:
                        pending = self._pending.pop(key, {})

                    if accepted and pending.get("result") == "ACCEPT":
                        with self._registry_lock:
                            self._sessions[node_id] = peer_id
                            self._sessions[peer_id] = node_id
                        # Notify requester (A)
                        self._send_raw(conn, write_lock, {
                            "type": "CONNECT_ACCEPTED", "peer": peer_id
                        })
                        # Notify acceptor (B) that session is live
                        self._send_to(peer_id, {
                            "type": "CONNECT_ESTABLISHED", "peer": node_id
                        })
                        logger.info("Session established", extra={
                            "alice": node_id, "bob": peer_id
                        })
                    else:
                        self._send_raw(conn, write_lock, {
                            "type": "CONNECT_REJECTED",
                            "reason": "peer_rejected_or_timeout",
                        })

                elif mtype == "ACCEPT":
                    from_node: str = msg["from_node"]
                    key = (from_node, node_id)
                    with self._registry_lock:
                        pending = self._pending.get(key)
                    if pending:
                        pending["result"] = "ACCEPT"
                        pending["event"].set()

                elif mtype == "REQUEST_EBITS":
                    peer_id = self._sessions.get(node_id)
                    if not peer_id:
                        self._send_raw(conn, write_lock, {
                            "type": "ERROR", "reason": "no_active_session"
                        })
                        continue
                    protocol = msg.get("protocol", self.config["protocol"])
                    noise = float(msg.get("injected_noise", self.config["injected_qber"]))
                    n_qubits = int(msg.get("n_qubits", self.config.get("n_qubits", 200)))
                    self._run_qkd_and_distribute(
                        alice_id=node_id,
                        bob_id=peer_id,
                        protocol=protocol,
                        noise=noise,
                        n_qubits=n_qubits,
                        alice_conn=conn,
                        alice_lock=write_lock,
                    )

        finally:
            reader.close()
            try:
                conn.close()
            except OSError:
                pass
            if node_id:
                with self._registry_lock:
                    self._registry.pop(node_id, None)
                    peer = self._sessions.pop(node_id, None)
                    if peer:
                        self._sessions.pop(peer, None)
                logger.info("Node disconnected", extra={"node_id": node_id})

    # ------------------------------------------------------------------
    # QKD simulation + distribution
    # ------------------------------------------------------------------

    def _run_qkd_and_distribute(
        self,
        alice_id: str,
        bob_id: str,
        protocol: str,
        noise: float,
        n_qubits: int,
        alice_conn: socket.socket,
        alice_lock: threading.Lock,
    ) -> None:
        for attempt in range(1, MAX_RETRIES + 1):
            alice_key, bob_key, qber, chsh, secure = self._simulate_qkd(
                protocol, noise, n_qubits
            )
            logger.info("QKD attempt", extra={
                "attempt": attempt, "protocol": protocol,
                "qber": round(qber, 4), "chsh": chsh, "secure": secure,
            })

            if secure:
                self._send_raw(alice_conn, alice_lock, {
                    "type": "EBIT_RESULT", "role": "alice",
                    "key": alice_key, "qber": qber, "chsh": chsh, "attempt": attempt,
                })
                self._send_to(bob_id, {
                    "type": "EBIT_RESULT", "role": "bob",
                    "key": bob_key, "qber": qber, "chsh": chsh, "attempt": attempt,
                })
                return

            if attempt == MAX_RETRIES:
                reason = "QBER_EXCEEDED" if protocol == "BB84" else "CHSH_FAILED"
                abort = {
                    "type": "SESSION_ABORTED",
                    "reason": reason,
                    "qber": qber,
                    "chsh": chsh,
                }
                self._send_raw(alice_conn, alice_lock, abort)
                self._send_to(bob_id, abort)
                logger.info("Session aborted", extra={
                    "reason": reason, "qber": qber, "chsh": chsh
                })
                return
            # else: attempt < MAX_RETRIES and not secure → loop and retry

    def _simulate_qkd(
        self, protocol: str, noise: float, n_qubits: int
    ) -> tuple[list[int], list[int], float, float | None, bool]:
        """
        Returns (alice_key, bob_key, qber, chsh, secure).

        Reuses quantum/ primitives directly — no duplication.
        For BB84: run_bb84() gives both sides' bits.
        For E91:  run_e91() gives CHSH; key material comes from a BB84-style
                  Z-basis measurement run on the same channel (standard E91 practice).
        """
        chsh: float | None = None

        if protocol == "E91":
            e91 = run_e91(
                n_pairs_per_setting=self.config.get("n_pairs_per_setting", 500)
            )
            chsh = e91.chsh_s
            if not e91.exceeds_classical:
                # Already failed; fill dummy key so we can still report qber
                return [], [], 0.0, chsh, False

        # BB84 (or E91 key-generation sub-step)
        bb84 = run_bb84(n_qubits=n_qubits, injected_noise=noise)
        qber = bb84.qber

        matching = [
            i for i in range(len(bb84.alice_bits))
            if bb84.alice_bases[i] == bb84.bob_bases[i]
        ]
        # Must use the same fraction as run_bb84() so check and key positions
        # are consistent: check = matching[:split], key = matching[split:]
        split = max(1, int(len(matching) * BB84_CHECK_FRACTION))
        key_positions = matching[split:]
        alice_key = [bb84.alice_bits[i] for i in key_positions]
        bob_key   = [bb84.bob_results[i] for i in key_positions]

        if protocol == "BB84":
            secure = qber <= BB84_QBER_THRESHOLD
        else:  # E91 — CHSH already passed; also check qber
            secure = qber <= BB84_QBER_THRESHOLD

        return alice_key, bob_key, qber, chsh, secure
