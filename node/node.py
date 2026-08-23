r"""
Node: Control-Plane Peer (Phase 2) + Dual-Channel Transmission (Phase 5)
========================================================================

One Node class covers both peer roles:
    Alice = sender/initiator, Bob = receiver/acceptor.
Role is not configured; it EMERGES from which handshake method is called:
    request_connection()   marks this node as Alice
    wait_for_connection()  marks this node as Bob
Both roles then call run_qkd(): Alice triggers the server, Bob only listens.

--------------------
Control vs Data Plane
--------------------
This module owns ONLY the control-plane TCP connection to the ebit server
(server/ebit_server.py): registry, connection handshake, QKD negotiation.
Payload traffic never touches this socket; callers open two separate
DataChannels (classical.transport) on data_port / quantum_port and hand them
to transmit_payload() / receive_payload().

--------------------------
Wire Protocol (control)
--------------------------
Newline-delimited JSON: one JSON object per "\n"-terminated line.
Vocabulary (server side defined in server/ebit_server.py):

    REGISTER         -> REGISTERED            announce node_id to registry
    LIST_NODES       -> {nodes: [...]}        discovery
    CONNECT_REQUEST  -> CONNECT_ACCEPTED      Alice asks; server relays/rejects
    NOTIFY_CONNECT   -> ACCEPT                Bob is woken and must accept
    (after ACCEPT)   -> CONNECT_ESTABLISHED   session live on both sides
    REQUEST_EBITS    -> EBIT_RESULT           Alice triggers QKD; server pushes
                     | SESSION_ABORTED        results to BOTH peers

Failure contract: any abort (QBER above threshold after retries, or CHSH <= 2
for E91) arrives as SESSION_ABORTED and is raised as SessionAbortedError
carrying reason plus measured values.

-----------
Components
-----------
SessionAbortedError:
    Structured abort (.reason/.qber/.chsh) so callers branch on attributes
    instead of parsing exception text.

QKDSession (dataclass):
    role     "alice" | "bob" as assigned by the server
    key      list[int], UNIT: bits of reconciled key material
    qber     float, dimensionless ratio in [0, 1]
    chsh     float | None, CHSH S parameter; None under BB84
    attempt  int, 1-based count of which retry produced the key

Node:
    Lifecycle: connect -> register -> handshake (picks role) -> run_qkd ->
    optionally transmit_payload / receive_payload -> close.

-----------
Integration
-----------
  - scripts/run_benchmark.py: drives the whole lifecycle; reads the private
    timing dicts for its q_xfer/c_xfer diagnostics.
  - transmission/echo_validation.py: alice_transfer/bob_transfer wrap
    transmit_payload/receive_payload plus the Phase 6 echo exchange.
  - tests/test_network.py: full two-node + server bring-up.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any

# Make the project root importable when run outside package context.
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from metrics.logger import get_logger

# Shared JSON logger; extra={} fields become top-level JSON keys on each line.
logger = get_logger("node")


class SessionAbortedError(Exception):
    r"""
    Raised by run_qkd() when the server reports SESSION_ABORTED.

    ----------
    Attributes
    ----------
    reason : str
        Server-supplied token/text describing why the session died.
    qber : float
        UNIT: dimensionless. Measured QBER at abort time.
    chsh : float | None
        CHSH S value when an E91 Bell test failed; None for BB84 aborts.

    The message embeds both metrics so tracebacks are self-explanatory
    without unwrapping attributes.
    """

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
    """Outcome of one successful QKD round; field units in module docstring."""

    role: str           # "alice" | "bob"
    key: list[int]
    qber: float
    chsh: float | None
    attempt: int        # which attempt succeeded (1 or 2)


class Node:
    r"""
    One peer: control-plane client plus dual-channel payload sender/receiver.

    ----------------
    State Ownership
    ----------------
      _sock / _reader   Control connection to the ebit server; _reader wraps
                        the socket as a line-buffered text file so each
                        readline() yields exactly one JSON message.
      _is_alice         TRI-STATE: None until a handshake runs, then bool.
                        Role assignment IS the handshake's side effect; every
                        later phase branches on it.
      session           QKDSession | None, populated only by run_qkd() success.
      last_received_payload   bytes | None; written by receive_payload(), read
                        by Phase 6's bob_transfer() echo step.
      _last_tx/_rx_timings    Monotonic durations (UNIT: seconds) keyed
                        q_start/q_end/c_start/c_end; consumed by benchmark
                        diagnostics, never by protocol logic.

    -----
    Notes
    -----
    Single-session object: close() ends the control connection; reconnect by
    building a fresh Node rather than reusing a closed one.
    """

    def __init__(self, node_id: str, config: dict[str, Any]) -> None:
        """
        Store identity/config WITHOUT network activity; nothing opens until
        connect(). Constructing both peers up front is side-effect free.

        ----------
        Parameters
        ----------
        node_id : str
            Registry name ("A", "B", ...); must be unique among peers
            registering against the same server.
        config : dict
            Keys read across this module's lifetime:
              host, server_port   control-plane endpoint
              injected_qber       default noise for run_qkd()
              protocol            "BB84" | "E91" requested from the server
              n_qubits            optional; server default is 200
        """
        self.node_id = node_id
        self.config = config
        self._sock: socket.socket | None = None
        self._reader = None
        self._is_alice: bool | None = None  # set after connect/wait_for_connection
        self.session: QKDSession | None = None
        # Phase 5 -- dual-channel transmission state
        self.last_received_payload: bytes | None = None
        self._last_tx_timings: dict = {}   # populated by transmit_payload()
        self._last_rx_timings: dict = {}   # populated by receive_payload()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        Open the control connection to (host, server_port).

        makefile("r") gives a buffered text view whose readline() returns on
        every newline: that is what turns a raw TCP stream into the
        newline-delimited JSON message protocol used by _send/_recv.
        """
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((self.config["host"], self.config["server_port"]))
        self._reader = self._sock.makefile("r")
        logger.info("Connected to server", extra={
            "node_id": self.node_id,
            "server": f"{self.config['host']}:{self.config['server_port']}",
        })

    def close(self) -> None:
        """
        Best-effort shutdown of reader then socket, in that order (closing
        the file first avoids a ResourceWarning from the socket's internal
        buffer). OSError is suppressed: closing is cleanup, and a failure
        here must not mask an in-flight exception.
        """
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
        """
        Serialize `msg` as ONE newline-terminated JSON line.

        sendall() loops until every byte is written (a bare send() may accept
        only part of the buffer). The trailing newline is the frame delimiter
        _recv()'s readline() splits on; both sides must agree or framing dies.
        """
        data = (json.dumps(msg) + "\n").encode()
        self._sock.sendall(data)

    def _recv(self) -> dict:
        """
        Block for exactly one JSON line; return it parsed.

        Empty line means the server performed an orderly shutdown mid-conversation;
        that is a protocol violation here, so it raises ConnectionError naming
        the node instead of returning garbage/None to callers who then index
        into a missing key anyway (fail-closed).
        """
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
        """
        Announce node_id to the server registry.

        Strict response checking: anything but REGISTERED is treated as fatal
        (a mis-registered node would poison every later phase silently).
        """
        self._send({"type": "REGISTER", "node_id": self.node_id})
        resp = self._recv()
        if resp["type"] != "REGISTERED":
            raise RuntimeError(f"[{self.node_id}] Registration failed: {resp}")
        logger.info("Registered", extra={"node_id": self.node_id})

    def list_nodes(self) -> list[str]:
        """Query current registry contents (peer discovery primitive)."""
        self._send({"type": "LIST_NODES"})
        resp = self._recv()
        return resp["nodes"]

    def wait_for_peer(self, peer_id: str, timeout: float = 10.0) -> bool:
        r"""
        Poll the registry until `peer_id` appears, or time out.

        ----------
        Parameters
        ----------
        peer_id : str
            Registry name to wait for (typically the other node).
        timeout : float
            UNIT: seconds. Upper bound via time.monotonic() deadline, immune
            to wall-clock adjustments.

        -------
        Returns
        -------
        bool
            True when seen; False on timeout (caller decides whether that is
            fatal, so no exception is raised here).

        -----
        Notes
        -----
        50 ms sleep between polls: fast enough for human-scale tests, slow
        enough not to hammer the server with LIST_NODES traffic. There is no
        push notification for registry joins; polling keeps the server dumb.
        """
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
        r"""
        ALICE side: ask the server to set up a session with `peer_id`.

        Blocks through the server-mediated exchange:
            CONNECT_REQUEST -> (relay) -> CONNECT_ACCEPTED | rejection
        Rejection raises ConnectionRefusedError carrying the server's reply.

        Side effect (the actual role assignment): _is_alice = True. Everything
        downstream, including run_qkd()'s behaviour, keys off this flag.
        """
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
        r"""
        BOB side: block until the server relays Alice's connection request.

        Three-step acceptance, all blocking:
          1. recv NOTIFY_CONNECT           (who wants to connect)
          2. send ACCEPT                   (consent)
          3. recv CONNECT_ESTABLISHED      (server confirms both sides live)
        Any deviation from the expected message types is a RuntimeError: the
        control protocol has no legitimate reason to deliver anything else at
        this point in Bob's lifecycle.

        Side effect: _is_alice = False.

        -------
        Returns
        -------
        str
            The requesting peer's registry name (Alice), useful for logging
            or pairing checks by the caller.
        """
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
        r"""
        Drive (Alice) or receive (Bob) the QKD phase.

        Role asymmetry:
          Alice sends REQUEST_EBITS carrying protocol/noise/qubit-count; this
          is what makes the server run the simulation.
          Bob sends nothing; the server pushes the result after Alice's
          request arrives. Both peers block in _recv() until their copy of
          the outcome shows up.

        ----------
        Parameters
        ----------
        injected_noise : float | None
            UNIT: dimensionless noise probability. Precedence: explicit arg
            beats config["injected_qber"]; only meaningful from Alice (the
            requester), since Bob's request-less path carries no parameters.

        -------
        Returns
        -------
        QKDSession
            Stored on self.session AND returned.

        --------------
        Failure Modes
        --------------
        RuntimeError   called before a handshake assigned a role, or server
                       sent an unexpected message type mid-phase.
        SessionAbortedError   server aborted (QBER above threshold after
                       retries, or CHSH <= 2 under E91); carries measured
                       values for metrics/logging.
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

        # Both Alice and Bob block here waiting for the server's verdict
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
    # Phase 5 -- Dual-channel transmission
    # ------------------------------------------------------------------
    # Why threads (not asyncio): all networking in this project is blocking
    # and thread-based. AerSimulator's compiled C extensions release the GIL,
    # giving genuine parallelism for the quantum work; asyncio would need an
    # event-loop thread anyway, adding complexity for no benefit.
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
        r"""
        Alice: split payload, send both segments CONCURRENTLY.

        Step 1 (sequential): metadata JSON goes first on the classical
        channel. Ordering matters: Bob must know segment boundaries before
        any segment bytes arrive, and the quantum channel has no room for a
        header without breaking its SDC byte-alignment.

        Step 2 (concurrent): quantum thread SDC-encodes + sends over
        quantum_dc while the classical thread AES-GCM-encrypts + sends over
        classical_dc. Neither depends on the other, so wall-clock time is
        max(q, c) instead of q + c.

        ----------
        Parameters
        ----------
        payload : bytes
            UNIT: bytes. Full plaintext payload about to be split.
        decision : SplitDecision
            From split_controller; supplies fractions and lengths.
        aes_key : bytes
            UNIT: bytes, 32-byte AES-256 key from derive_key().
        available_ebits : int
            UNIT: ebits. Ceiling for the quantum segment's SDC encoding.

        -----
        Notes
        -----
        Error containment: each thread catches into `errors` and ALWAYS
        records its end-timing in `finally`, so both join cleanly even on
        failure (no leaked threads, complete timings); the first error is
        re-raised only after the join, preserving determinism.
        Timing lands in _last_tx_timings (monotonic seconds, 4-dp rounded in
        logs) for the benchmark's concurrency assertions.
        """
        from transmission.payload_splitter import split_payload, make_metadata
        from transmission.quantum_transmit import encode_bytes_sdc
        from transmission.classical_transmit import send_classical_segment

        split = split_payload(payload, decision)
        meta = make_metadata(split)

        # Step 1 -- metadata (negligible size; sent before concurrent phase)
        classical_dc.send(json.dumps(meta).encode())

        # Step 2 -- concurrent send
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

        # Raise AFTER joining so timings are complete and no thread leaks;
        # quantum errors take precedence (rarer, more interesting failures).
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
        r"""
        Bob: receive both segments concurrently, reassemble, store result.

        Mirror of transmit_payload():
          Step 1 (sequential): read metadata JSON from classical_dc; it names
                               the byte boundary BEFORE either thread starts,
                               so validation can happen even if a thread dies.
          Step 2 (concurrent): quantum thread blocks on quantum_dc.recv() raw
                               bytes; classical thread does recv +
                               AES-GCM decrypt (InvalidTag propagates as a
                               caught error, re-raised after join).
          Step 3: reassemble quantum_segment + classical_segment per metadata
                  and validate lengths (payload_splitter owns those rules).

        -------
        Returns
        -------
        bytes
            The reassembled payload; ALSO stored on
            self.last_received_payload because Phase 6's bob_transfer() reads
            it back for the echo step after this call returns.

        -----
        Notes
        -----
        Same error-containment contract as transmit_payload(): errors are
        captured per-thread, joined, then re-raised; timings land in
        _last_rx_timings.
        """
        from transmission.payload_splitter import reassemble_from_segments
        from transmission.classical_transmit import recv_classical_segment

        # Step 1 -- metadata
        meta = json.loads(classical_dc.recv().decode())

        # Step 2 -- concurrent receive
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

        # Step 3 -- reassemble and validate
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
