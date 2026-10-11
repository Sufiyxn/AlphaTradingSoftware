"""A minimal RFC 6455 WebSocket client built only on the Python standard library.

The platform deliberately keeps its runtime dependency-free, so streaming
market data cannot lean on a third-party socket library. This module implements
exactly the subset a market-data stream needs and nothing more:

* the opening handshake (``GET`` + ``Upgrade: websocket``) with custom request
  headers so a provider can authenticate during the handshake;
* masked client text frames and unmasked server frames, per RFC 6455;
* automatic ``ping``/``pong`` handling, close frames, and fragmentation;
* a hard message-size ceiling so a runaway feed cannot exhaust memory.

It performs no protocol interpretation of its own: the market-data stream layer
decides what a message means. Nothing here fabricates data - a broken handshake,
a rejected frame, or a dropped socket raises; it never returns a guess.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-5AB0DC85B11A"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

DEFAULT_MAX_MESSAGE_BYTES = 1_000_000
_HANDSHAKE_READ_LIMIT = 64 * 1024
_RESERVED_HANDSHAKE_HEADERS = frozenset({
    "host", "upgrade", "connection", "sec-websocket-key",
    "sec-websocket-version", "sec-websocket-protocol",
})


class WebSocketError(Exception):
    """Base class for every WebSocket failure."""


class WebSocketHandshakeError(WebSocketError):
    """The opening handshake failed or was rejected by the server."""


class WebSocketProtocolError(WebSocketError):
    """A frame violated RFC 6455."""


class WebSocketClosed(WebSocketError):
    """The peer closed the connection (close frame or EOF)."""


class WebSocketClient:
    """One connected WebSocket. Not thread-safe: one reader at a time."""

    def __init__(self, sock: socket.socket, *, url: str,
                 max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> None:
        if max_message_bytes <= 0:
            raise ValueError("max_message_bytes must be positive")
        self._sock = sock
        self.url = url
        self.max_message_bytes = int(max_message_bytes)
        self._buffer = bytearray()
        self._closed = False
        self._close_sent = False

    # -- construction -----------------------------------------------------
    @classmethod
    def connect(cls, url: str, *, timeout: float = 10.0,
                headers: dict[str, str] | None = None,
                subprotocols: tuple[str, ...] = (),
                max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
                ssl_context: ssl.SSLContext | None = None) -> "WebSocketClient":
        """Open the socket and complete the opening handshake.

        Raises :class:`WebSocketHandshakeError` when the server refuses or the
        response does not match RFC 6455, and ``OSError``/``socket.timeout``
        when the endpoint cannot be reached at all.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketHandshakeError(
                f"unsupported WebSocket scheme {parsed.scheme!r}; expected ws or wss")
        if not parsed.hostname:
            raise WebSocketHandshakeError("the WebSocket URL has no host")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        secure = parsed.scheme == "wss"
        port = parsed.port or (443 if secure else 80)

        raw = socket.create_connection((parsed.hostname, port), timeout=timeout)
        sock = raw
        try:
            if secure:
                context = ssl_context or ssl.create_default_context()
                sock = context.wrap_socket(raw, server_hostname=parsed.hostname)
            sock.settimeout(timeout)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            remainder = cls._handshake(sock, parsed.hostname, port, path, key,
                                       headers=headers, subprotocols=subprotocols,
                                       secure=secure)
            client = cls(sock, url=url, max_message_bytes=max_message_bytes)
            if remainder:
                client._buffer.extend(remainder)
            return client
        except BaseException:
            try:
                raw.close()
            except OSError:
                pass
            if sock is not raw:
                try:
                    sock.close()
                except OSError:
                    pass
            raise

    @staticmethod
    def _handshake(sock: socket.socket, host: str, port: int, path: str,
                   key: str, *, headers: dict[str, str] | None,
                   subprotocols: tuple[str, ...], secure: bool) -> bytes:
        default_port = 443 if secure else 80
        host_header = host if port == default_port else f"{host}:{port}"
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host_header}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if subprotocols:
            lines.append("Sec-WebSocket-Protocol: " + ", ".join(subprotocols))
        for name, value in (headers or {}).items():
            if name.lower() in _RESERVED_HANDSHAKE_HEADERS:
                raise WebSocketHandshakeError(
                    f"the header {name!r} is managed by the WebSocket handshake")
            lines.append(f"{name}: {value}")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        sock.sendall(request)

        chunks = bytearray()
        while b"\r\n\r\n" not in chunks:
            block = sock.recv(4096)
            if not block:
                raise WebSocketHandshakeError(
                    "the server closed the connection during the opening handshake")
            chunks.extend(block)
            if len(chunks) > _HANDSHAKE_READ_LIMIT:
                raise WebSocketHandshakeError("the opening handshake response is too large")
        head, _, rest = bytes(chunks).partition(b"\r\n\r\n")
        parts = head.split(b"\r\n")
        status_line, header_lines = parts[0], parts[1:]
        if not status_line.startswith(b"HTTP/1.1 101") and not status_line.startswith(b"HTTP/1.0 101"):
            raise WebSocketHandshakeError(
                "the server refused the upgrade: "
                + status_line.decode("latin-1", "replace"))
        received: dict[str, str] = {}
        for line in header_lines:
            name, sep, value = line.partition(b":")
            if sep:
                received[name.decode("latin-1").strip().lower()] = \
                    value.decode("latin-1").strip()
        if "websocket" not in received.get("upgrade", "").lower():
            raise WebSocketHandshakeError("the server did not answer with Upgrade: websocket")
        if "upgrade" not in received.get("connection", "").lower():
            raise WebSocketHandshakeError("the server did not answer with Connection: Upgrade")
        expected = base64.b64encode(
            hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
        if received.get("sec-websocket-accept", "") != expected:
            raise WebSocketHandshakeError(
                "the Sec-WebSocket-Accept header does not match the request key")
        return rest

    # -- message IO -------------------------------------------------------
    def send_text(self, text: str) -> None:
        if self._closed:
            raise WebSocketClosed("the WebSocket is closed")
        if not isinstance(text, str):
            raise TypeError("send_text expects a str")
        payload = text.encode("utf-8")
        if len(payload) > self.max_message_bytes:
            raise WebSocketProtocolError("refusing to send an oversized message")
        self._send_frame(OP_TEXT, payload)

    def recv(self, timeout: float | None = None) -> str:
        """Read the next complete text message.

        Ping and close frames are handled internally. ``TimeoutError`` is
        raised when ``timeout`` elapses with no complete message, so a caller
        can keep polling its stop flag; :class:`WebSocketClosed` means the
        connection is gone for good.
        """
        if self._closed:
            raise WebSocketClosed("the WebSocket is closed")
        previous = self._sock.gettimeout()
        if timeout is not None:
            self._sock.settimeout(timeout)
        try:
            while True:
                fin, opcode, payload = self._read_frame()
                if opcode == OP_PING:
                    self._send_frame(OP_PONG, payload)
                    continue
                if opcode == OP_PONG:
                    continue
                if opcode == OP_CLOSE:
                    self._closed = True
                    self._best_effort_close_reply(payload)
                    raise WebSocketClosed("the server closed the connection")
                if opcode in (OP_TEXT, OP_BINARY):
                    message = payload if fin else self._read_continuation(payload)
                    if len(message) > self.max_message_bytes:
                        raise WebSocketProtocolError("the message exceeds the size ceiling")
                    if opcode == OP_BINARY:
                        raise WebSocketProtocolError(
                            "binary frames are not part of the provider protocol")
                    return message.decode("utf-8")
                raise WebSocketProtocolError(f"unsupported frame opcode {opcode:#x}")
        except (socket.timeout, TimeoutError):
            raise TimeoutError("no WebSocket message arrived within the timeout") from None
        except EOFError:
            raise WebSocketClosed("the connection ended unexpectedly") from None
        finally:
            if timeout is not None and not self._closed:
                try:
                    self._sock.settimeout(previous)
                except OSError:
                    pass

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        payload = struct.pack("!H", code) + reason.encode("utf-8")[:120]
        try:
            if not self._close_sent:
                self._close_sent = True
                self._send_frame(OP_CLOSE, payload)
        except (OSError, WebSocketError, ValueError):
            pass
        finally:
            self._closed = True
            try:
                self._sock.close()
            except OSError:
                pass

    def __enter__(self) -> "WebSocketClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- internals --------------------------------------------------------
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        try:
            self._sock.sendall(bytes(header) + masked)
        except (OSError, socket.timeout, ValueError) as error:
            self._closed = True
            raise WebSocketClosed("the connection was lost while sending") from error

    def _recv_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            try:
                block = self._sock.recv(max(4096, count - len(self._buffer)))
            except (socket.timeout, TimeoutError):
                raise
            except (OSError, ValueError) as error:
                self._closed = True
                raise WebSocketClosed("the connection was lost while reading") from error
            if not block:
                self._closed = True
                raise EOFError("the peer closed the connection")
            self._buffer.extend(block)
        chunk = bytes(self._buffer[:count])
        del self._buffer[:count]
        return chunk

    def _read_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._recv_exact(2)
        fin = bool(first & 0x80)
        if first & 0x70:
            raise WebSocketProtocolError("reserved frame bits are set")
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._recv_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._recv_exact(8))
        if length > self.max_message_bytes:
            raise WebSocketProtocolError("the frame exceeds the size ceiling")
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    def _read_continuation(self, first: bytes) -> bytes:
        message = bytearray(first)
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self._closed = True
                self._best_effort_close_reply(payload)
                raise WebSocketClosed("the server closed the connection mid-message")
            if opcode != OP_CONT:
                raise WebSocketProtocolError("unexpected opcode inside a fragmented message")
            message.extend(payload)
            if len(message) > self.max_message_bytes:
                raise WebSocketProtocolError("the message exceeds the size ceiling")
            if fin:
                return bytes(message)

    def _best_effort_close_reply(self, payload: bytes) -> None:
        if self._close_sent:
            return
        self._close_sent = True
        try:
            self._send_frame(OP_CLOSE, payload[:125])
        except (OSError, WebSocketError):
            pass
        try:
            self._sock.close()
        except OSError:
            pass
