r"""
TCP Transport for the Direct A<->B Classical Data Channel
=========================================================

Thin message-framing layer over a raw TCP stream. Provides NO security of its
own: confidentiality/integrity come from the application layer above it
(classical/aes_channel.py AES-256-GCM), which is deliberate layering, this
module only solves TCP's lack of message boundaries.

-------
Framing
-------
TCP is a byte stream: one send() may arrive as several recv() chunks, or
several sends may coalesce into one. Every message is therefore wrapped in a
length-prefixed frame so arbitrary-size payloads survive any fragmentation:

    [ length: uint32 big-endian (4 bytes) ][ payload: `length` bytes ]

The length field counts PAYLOAD BYTES ONLY (header excluded). Big-endian is
network byte order, the cross-platform convention. Max frame = 2^32 - 1 bytes;
payload sizes here are far below that ceiling.

-----------
Components
-----------
send_frame(sock, data) / recv_frame(sock):
    Stateless frame codec operating on ANY socket. Kept separate from
    DataChannel so tests can drive them over bare socketpairs without the
    listen/accept dance (every tests/*.py does exactly this).

_recv_exact(sock, n):
    Loop-until-n receive helper. The core correctness primitive: socket.recv()
    may legally return FEWER bytes than requested even mid-stream (kernel
    buffer limits, scheduling), so a single recv() call is never sufficient.

DataChannel:
    One side of the direct Bob-listens/Alice-connects TCP socket. A convenience
    state machine (listen -> accept -> send/recv -> close, or connect ->
    send/recv -> close) around the frame codec.

------------------
Channel Topology
------------------
Each session opens TWO independent DataChannel instances on different ports:

    data_port    : encrypted payload segments (Phase 5), Bob's echo and Alice's
                   patches (Phase 6, echo_validation.py)
    quantum_port : quantum-protocol classical traffic, basis sifting rounds
                   (server) and Cascade parity exchanges (reconciliation.py)

Both carry only ciphertext/authenticated protocol messages; neither touches
the control-plane Node<->Server sockets owned by node/node.py.

-----------
Integration
-----------
  - transmission/classical_transmit.py: send_classical_segment() =
    aes_channel.encrypt(data) -> dc.send(); the transport therefore always
    carries GCM ciphertext, never plaintext (fault_injector corrupts upstream,
    pre-encryption).
  - node/node.py: creates both channels and threads them through transmit/
    receive paths.
  - quantum/reconciliation.py: dc.send/dc.recv used for parity messages.
  - transmission/echo_validation.py: same classical channel reused for echo.
  - tests/*: send_frame/recv_frame over socketpairs; test_classical_channel.py
    exercises DataChannel end-to-end.

--------------
Threading Model
--------------
Fully blocking, single-threaded, lockstep: each phase's send has a matching
recv on the peer. No timeouts are set, a missing peer blocks forever by
design (the orchestrator controls liveness).

--------------
Security Notes
--------------
- No TLS: wire carries only AES-256-GCM ciphertext produced upstream; adding
  TLS would be redundant for this threat model and is intentionally absent.
- ConnectionError from _recv_exact surfaces truncation explicitly rather than
  silently returning short data, fail-closed behaviour.
"""

from __future__ import annotations

import socket
import struct

# Public API: everything else is implementation detail.
__all__ = ["DataChannel", "send_frame", "recv_frame"]

# Frame header format: ">" = big-endian (network byte order, portable across
# architectures), "I" = unsigned 32-bit int. UNIT: bytes of the FOLLOWING
# payload, header itself excluded. Range [0, 2^32 - 1].
_HEADER_FMT = ">I"

# UNIT: bytes (= 4). Derived once from the format string so header size and
# format can never drift apart; recv_frame uses it to read exactly one header.
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)


def send_frame(sock: socket.socket, data: bytes) -> None:
    r"""
    Send `data` as one length-prefixed frame.

    ----------
    Parameters
    ----------
    sock : socket.socket
        Connected TCP socket (either direction; the protocol is symmetric).
    data : bytes
        UNIT: bytes. Arbitrary length up to 2^32 - 1. Empty payloads are
        valid: they encode as a bare 4-byte zero header.

    -----------------
    Why sendall()
    -----------------
    socket.send() may accept only part of the buffer (kernel send buffer
    full). sendall() loops until every byte is written, guaranteeing the
    receiver sees an intact frame. Header and payload go out in ONE sendall
    so small messages typically coalesce into a single TCP segment.
    """
    header = struct.pack(_HEADER_FMT, len(data))
    sock.sendall(header + data)


def recv_frame(sock: socket.socket) -> bytes:
    r"""
    Receive exactly one length-prefixed frame.

    ----------
    Parameters
    ----------
    sock : socket.socket
        Connected TCP socket.

    -------
    Returns
    -------
    bytes
        The exact payload written by the peer's send_frame(), no more, no
        less, regardless of how TCP fragmented the stream. UNIT: bytes.

    ---------------
    Failure Modes
    ---------------
    Raises ConnectionError if the peer closes mid-frame (truncated header or
    truncated payload): partial delivery is NEVER returned as success.
    Blocks indefinitely until a full frame arrives (no timeout set).
    """
    # Step 1: read the 4-byte header (looping, may arrive split).
    header = _recv_exact(sock, _HEADER_SIZE)
    # Step 2: decode payload length, then read exactly that many bytes.
    (length,) = struct.unpack(_HEADER_FMT, header)
    return _recv_exact(sock, length)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    r"""
    Read exactly `n` bytes from `sock`, tolerating short reads.

    ----------
    Parameters
    ----------
    sock : socket.socket
        Connected TCP socket.
    n : int
        UNIT: bytes. Exact number of bytes required before returning.

    -------
    Returns
    -------
    bytes
        Exactly `n` bytes. UNIT: bytes.

    -----
    Notes
    -----
    TCP guarantees ordering but not message boundaries: recv(n) returns
    "up to n" bytes, whatever is currently buffered. The loop accumulates
    chunks until the quota is met. An empty chunk means the peer performed an
    orderly shutdown; mid-frame that is a protocol violation, so it raises
    ConnectionError carrying progress diagnostics (received vs expected)
    instead of silently returning short data.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(
                f"Connection closed after {len(buf)} of {n} expected bytes"
            )
        buf.extend(chunk)
    return bytes(buf)


class DataChannel:
    r"""
    One side of the direct TCP socket between Node A (Alice) and Node B (Bob).

    Role assignment (asymmetric setup, symmetric use):
        Bob   = listener: listen() then accept()  (blocks until Alice arrives)
        Alice = connector: connect()
    After the handshake both peers hold equivalent send()/recv() capability -
    the class does not enforce who talks first; the protocol phases do.

    State machine:
        constructed          -> host/port stored, no sockets open
        listen()             -> _listener bound (Bob only)
        accept() / connect() -> _conn live; send/recv become legal
        close()              -> both sockets shut; back to inert

    --------
    Bob side
    --------
        dc = DataChannel("127.0.0.1", data_port)
        dc.listen()   # binds and starts listening
        dc.accept()   # blocks until Alice connects
        data = dc.recv()
        dc.close()

    ----------
    Alice side
    ----------
        dc = DataChannel("127.0.0.1", data_port)
        dc.connect()  # connects to Bob
        dc.send(data)
        dc.close()

    -----
    Notes
    -----
    Instances are single-use: after close(), reconnect by building a new
    DataChannel (keeps cleanup logic trivial and leak-free).
    """

    def __init__(self, host: str, port: int) -> None:
        """
        Store endpoint parameters WITHOUT opening anything (I/O starts at
        listen()/connect(), so constructing both peers' objects up front is
        side-effect free).

        ----------
        Parameters
        ----------
        host : str
            IP address or hostname to bind (Bob) / connect to (Alice),
            e.g. "127.0.0.1".
        port : int
            UNIT: TCP port number, range [0, 65535]. 0 is meaningful only for
            Bob: listen() replaces it with the OS-assigned ephemeral port.

        ---------------------
        Attributes (set here)
        ---------------------
        _listener : socket.socket | None
            Listening socket (Bob only). None until listen().
        _conn : socket.socket | None
            The live data connection. None until accept()/connect(); reset to
            None by close(). All send/recv traffic flows through this one.
        """
        self.host = host
        self.port = port
        self._listener: socket.socket | None = None
        self._conn: socket.socket | None = None

    # ------------------------------------------------------------------
    # Bob side
    # ------------------------------------------------------------------

    def listen(self) -> int:
        r"""
        Bind and listen on (host, port). Bob-side only, before accept().

        -------
        Returns
        -------
        int
            UNIT: TCP port actually bound. Differs from self.port when the
            caller passed 0 for an OS-assigned ephemeral port, the standard
            trick for collision-free ports in parallel tests; callers must
            propagate this value to Alice out-of-band.

        -------------------------
        Socket Options & Backlog
        -------------------------
        SO_REUSEADDR: lets the same port rebind immediately after a prior
        socket sits in TIME_WAIT, without it, rapid test/benchmark restarts
        would intermittently fail with "Address already in use".

        backlog=1: exactly one pending connection ever expected (single-peer
        protocol); a larger queue would just mask accidental extra clients.
        """
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self.host, self.port))
        self._listener.listen(1)
        actual_port = self._listener.getsockname()[1]
        self.port = actual_port   # keep attribute truthful for OS-assigned ports
        return actual_port

    def accept(self) -> None:
        r"""
        Block until the peer (Alice) connects and store the data connection.
        Must call listen() first, enforced with RuntimeError rather than an
        opaque AttributeError deeper in.

        Populates `_conn`, which flips the object into its usable state (the
        same state Alice reaches via connect()); afterwards the Bob/Alice role
        distinction disappears.
        """
        if self._listener is None:
            raise RuntimeError("call listen() before accept()")
        self._conn, _ = self._listener.accept()

    # ------------------------------------------------------------------
    # Alice side
    # ------------------------------------------------------------------

    def connect(self) -> None:
        r"""
        Connect to Bob's data listener at (host, port). Alice-side only.

        Blocks until the TCP three-way handshake completes (i.e. until Bob is
        listening). On success populates `_conn`, the same post-condition as
        Bob's accept().
        """
        self._conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._conn.connect((self.host, self.port))

    # ------------------------------------------------------------------
    # Shared send / recv
    # ------------------------------------------------------------------

    def send(self, data: bytes) -> None:
        r"""
        Send one framed message over the established connection.

        ----------
        Parameters
        ----------
        data : bytes
            UNIT: bytes. Passed straight to send_frame(); typically AES-GCM
            ciphertext produced by classical_transmit.send_classical_segment.
        """
        if self._conn is None:
            raise RuntimeError("not connected, call connect() or accept() first")
        send_frame(self._conn, data)

    def recv(self) -> bytes:
        r"""
        Receive one framed message (blocks until fully arrived).

        -------
        Returns
        -------
        bytes
            UNIT: bytes. Exactly what the peer passed to send().

        -------------
        Failure Modes
        -------------
        ConnectionError propagates from recv_frame() if the peer dies
        mid-frame; RuntimeError if called before the connection exists.
        """
        if self._conn is None:
            raise RuntimeError("not connected, call connect() or accept() first")
        return recv_frame(self._conn)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        r"""
        Shut down whichever sockets exist; safe to call in any state.

        Design details:
        - Handles half-built lifecycles (listen()-only Bob, or never-connected
          Alice) by skipping None sockets.
        - Suppresses OSError from double-close: closing is best-effort cleanup,
          and a failure here must not mask the real exception in flight.
        - Nulls BOTH references so any subsequent send()/recv() fails fast with
          the clear "not connected" RuntimeError instead of the confusing
          "Operation on closed socket" OSError, and so the object cannot
          resurrect a dead connection.
        """
        for sock in (self._conn, self._listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._conn = None
        self._listener = None
