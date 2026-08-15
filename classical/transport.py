"""
TCP transport for the direct A<->B classical data channel.

Framing
-------
Each payload is prefixed with a 4-byte big-endian uint32 length field so that
arbitrary-size payloads work correctly regardless of how TCP fragments the stream.

    [ length: uint32 BE (4 bytes) ][ payload: length bytes ]

DataChannel
-----------
Manages one side of the direct Bob-listens / Alice-connects TCP socket.
Bob calls listen() then accept(); Alice calls connect(). Both then use send()/recv().
The control-plane Node<->Server sockets are entirely separate and unaffected.
"""

import socket
import struct

__all__ = ["DataChannel", "send_frame", "recv_frame"]

_HEADER_FMT  = ">I"    # big-endian unsigned 32-bit integer
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)


def send_frame(sock: socket.socket, data: bytes) -> None:
    """Send data with a 4-byte length prefix. Handles arbitrary payload size."""
    header = struct.pack(_HEADER_FMT, len(data))
    sock.sendall(header + data)


def recv_frame(sock: socket.socket) -> bytes:
    """Receive a length-prefixed frame. Blocks until all bytes arrive."""
    header = _recv_exact(sock, _HEADER_SIZE)
    (length,) = struct.unpack(_HEADER_FMT, header)
    return _recv_exact(sock, length)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
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
    """
    Direct TCP socket between Node A (Alice) and Node B (Bob).

    Bob (listener) usage:
        dc = DataChannel("127.0.0.1", data_port)
        dc.listen()   # binds and starts listening
        dc.accept()   # blocks until Alice connects
        data = dc.recv()
        dc.close()

    Alice (connector) usage:
        dc = DataChannel("127.0.0.1", data_port)
        dc.connect()  # connects to Bob
        dc.send(data)
        dc.close()
    """

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._listener: socket.socket | None = None
        self._conn: socket.socket | None = None

    # ------------------------------------------------------------------
    # Bob side
    # ------------------------------------------------------------------

    def listen(self) -> int:
        """
        Bind and listen on (host, port). Returns the actual port bound
        (useful if port=0 was given for OS-assigned ephemeral port).
        """
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self.host, self.port))
        self._listener.listen(1)
        actual_port = self._listener.getsockname()[1]
        self.port = actual_port   # update in case OS assigned a port
        return actual_port

    def accept(self) -> None:
        """Block until the peer (Alice) connects. Must call listen() first."""
        if self._listener is None:
            raise RuntimeError("call listen() before accept()")
        self._conn, _ = self._listener.accept()

    # ------------------------------------------------------------------
    # Alice side
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Connect to Bob's data listener at (host, port)."""
        self._conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._conn.connect((self.host, self.port))

    # ------------------------------------------------------------------
    # Shared send / recv
    # ------------------------------------------------------------------

    def send(self, data: bytes) -> None:
        if self._conn is None:
            raise RuntimeError("not connected — call connect() or accept() first")
        send_frame(self._conn, data)

    def recv(self) -> bytes:
        if self._conn is None:
            raise RuntimeError("not connected — call connect() or accept() first")
        return recv_frame(self._conn)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        for sock in (self._conn, self._listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._conn = None
        self._listener = None
