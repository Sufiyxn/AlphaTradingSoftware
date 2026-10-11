"""A loopback WebSocket server for automated tests only.

Nothing in :mod:`backend.core.runtime` imports this module. It exists so the
streaming layer can be exercised against a real RFC 6455 socket - handshake,
masking, fragmentation, ping/pong, close frames, abrupt drops - without any
network access and without a vendor credential.

The test drives the conversation through a ``handler(conn)`` callable: the
handler decides which frames to send (including fabricated *test* quotes) and
when to drop the connection, which is how reconnect, authentication failure,
and subscription failure paths are proven deterministically.
"""
from __future__ import annotations

import json
import socket
import struct
import threading

from backend.market_data.ws import (
    OP_CLOSE,
    OP_CONT,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    expected_accept,
)

_HANDSHAKE_LIMIT = 64 * 1024


class ServerConnection:
    """One accepted test connection; the handler runs on a worker thread."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._send_lock = threading.Lock()
        self.received: list[str] = []
        self.closed = threading.Event()

    def send(self, message: str) -> None:
        """Send one unmasked server text frame."""
        payload = message.encode("utf-8")
        length = len(payload)
        header = bytearray([0x81])
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        with self._send_lock:
            if self.closed.is_set():
                raise ConnectionError("the test connection is closed")
            self._sock.sendall(bytes(header) + payload)

    def send_json(self, payload) -> None:
        self.send(json.dumps(payload))

    def recv(self, timeout: float = 5.0) -> str:
        """Read the next client text message, answering pings automatically."""
        self._sock.settimeout(timeout)
        message_parts: list[bytes] = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == OP_PING:
                self._control(0x8A, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self._control(0x88, payload[:125])
                raise ConnectionClosed("the client closed the connection")
            if opcode in (OP_TEXT, OP_CONT):
                message_parts.append(payload)
                if fin:
                    return b"".join(message_parts).decode("utf-8")
                continue
            raise AssertionError(f"unexpected client opcode {opcode:#x}")

    def recv_json(self, timeout: float = 5.0):
        return json.loads(self.recv(timeout=timeout))

    def recv_all(self, count: int, timeout: float = 5.0) -> list[str]:
        return [self.recv(timeout=timeout) for _ in range(count)]

    def drop(self) -> None:
        """Close the socket without a close frame (an abrupt disconnect)."""
        self.closed.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()

    def close(self) -> None:
        try:
            self._control(0x88, b"")
        except (OSError, ConnectionError):
            pass
        self.drop()

    # -- frame helpers ----------------------------------------------------
    def _control(self, opcode: int, payload: bytes) -> None:
        with self._send_lock:
            if self.closed.is_set():
                return
            self._sock.sendall(bytes([0x80 | opcode, len(payload)]) + payload)

    def _recv_exact(self, count: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < count:
            block = self._sock.recv(count - len(chunks))
            if not block:
                raise ConnectionClosed("the client stopped sending")
            chunks.extend(block)
        return bytes(chunks)

    def _read_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._recv_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._recv_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._recv_exact(8))
        if length > 4_000_000:
            raise AssertionError("test client sent an absurd frame")
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload


class ConnectionClosed(Exception):
    """Raised inside a handler when the peer has gone away."""


class WebSocketTestServer:
    """Loopback RFC 6455 server. ``start()`` returns a ``ws://`` URL."""

    def __init__(self, handler, *, accept_key=None, status_line: str = "HTTP/1.1 101 Switching Protocols") -> None:
        self._handler = handler
        self._accept_key = accept_key
        self._status_line = status_line
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        self.connections: list[ServerConnection] = []
        self.handshake_headers: list[dict[str, str]] = []
        self.handler_errors: list[BaseException] = []
        self.handshakes = 0
        self._stopping = threading.Event()
        self._accept_thread = threading.Thread(target=self._accept_loop,
                                               name="alpha-ws-test-server", daemon=True)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/stocks"

    def start(self) -> str:
        self._accept_thread.start()
        return self.url

    def stop(self) -> None:
        self._stopping.set()
        try:
            self._listener.close()
        except OSError:
            pass
        for connection in list(self.connections):
            connection.drop()

    def __enter__(self) -> "WebSocketTestServer":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def wait_for_connections(self, count: int, timeout: float = 5.0) -> bool:
        deadline = threading.Event()
        waited = 0.0
        while waited < timeout:
            if len(self.connections) >= count:
                return True
            deadline.wait(0.02)
            waited += 0.02
        return len(self.connections) >= count

    def wait_for_messages(self, index: int, count: int, timeout: float = 5.0) -> bool:
        """Wait until connection ``index`` has received at least ``count`` messages."""
        waited = 0.0
        while waited < timeout:
            if index < len(self.connections) and len(self.connections[index].received) >= count:
                return True
            threading.Event().wait(0.02)
            waited += 0.02
        return False

    # -- internals --------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                sock, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock: socket.socket) -> None:
        try:
            sock.settimeout(5.0)
            request = self._read_handshake(sock)
            if request is None:
                return
            key = request.get("sec-websocket-key", "")
            accept = self._accept_key or expected_accept(key)
            response = (
                f"{self._status_line}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode("ascii")
            if not self._status_line.startswith("HTTP/1.1 101"):
                sock.sendall(response)
                sock.close()
                return
            sock.sendall(response)
            self.handshakes += 1
            self.handshake_headers.append(dict(request or {}))
            connection = ServerConnection(sock)
            self.connections.append(connection)
            try:
                self._handler(connection)
            except (ConnectionClosed, ConnectionError, OSError, BrokenPipeError):
                pass
            except BaseException as error:  # noqa: BLE001 - surfaced to the test
                self.handler_errors.append(error)
            finally:
                connection.drop()
        except (OSError, ConnectionClosed):
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _read_handshake(sock: socket.socket) -> dict[str, str] | None:
        chunks = bytearray()
        while b"\r\n\r\n" not in chunks:
            block = sock.recv(4096)
            if not block:
                return None
            chunks.extend(block)
            if len(chunks) > _HANDSHAKE_LIMIT:
                return None
        head, _, _ = bytes(chunks).partition(b"\r\n\r\n")
        headers: dict[str, str] = {}
        lines = head.split(b"\r\n")
        for line in lines[1:]:
            name, sep, value = line.partition(b":")
            if sep:
                headers[name.decode("latin-1").strip().lower()] = \
                    value.decode("latin-1").strip()
        return headers
