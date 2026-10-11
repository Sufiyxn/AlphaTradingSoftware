"""Streaming market-data service: one normalized live-quote source of truth.

This module owns the *push* half of market data. REST adapters answer "what was
the latest value?" on demand; a stream keeps a live socket open and pushes
trades as they happen, which is what a real-time trading product needs.

Design rules (they are the same rules as the REST path):

* **No fabrication.** Every cached price comes from a message the provider
  actually sent. A missing symbol stays missing, a malformed event is counted
  and dropped, and nothing is ever interpolated, averaged, or defaulted.
* **Provider timestamps are preserved.** A quote keeps the vendor's observation
  time (converted to UTC) so downstream staleness checks age the data against
  the *market's* clock, not the socket's.
* **Honest state.** The snapshot reports what really happened: ``CONNECTED``
  only after the handshake, authentication, and subscription all succeeded,
  ``AUTH_FAILED``/``SUBSCRIPTION_FAILED`` when the vendor said no, and
  ``RECONNECTING`` with the real reason while a dropped socket is being
  restored. Credential presence alone never reports a connected stream.
* **One consumer surface.** Quotes are read through
  :class:`~backend.market_data.service.MarketDataService`, so charts, the
  watchlist, the scanner, position valuation, and the risk engine all see the
  same normalized data at the same freshness.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from .models import Quote
from .validation import DataValidationError, normalize_quote
from .ws import WebSocketClient, WebSocketError

LOG = logging.getLogger(__name__)

STATE_OFF = "OFF"
STATE_NOT_CONFIGURED = "NOT_CONFIGURED"
STATE_CONNECTING = "CONNECTING"
STATE_CONNECTED = "CONNECTED"
STATE_RECONNECTING = "RECONNECTING"
STATE_AUTH_FAILED = "AUTH_FAILED"
STATE_SUBSCRIPTION_FAILED = "SUBSCRIPTION_FAILED"
STATE_ERROR = "ERROR"
STATE_STOPPED = "STOPPED"

#: States that mean "live events are flowing into the platform right now".
STREAMING_STATES = frozenset({STATE_CONNECTED})

REASON_NO_PROVIDER = "no market-data provider with a streaming feed is configured"
REASON_DISABLED = "streaming is disabled by MARKET_DATA_STREAM=off"
REASON_NO_CREDENTIALS = "the provider has no API key, so the stream cannot authenticate"

_MAX_DETAIL = 300
_RECV_POLL_SECONDS = 1.0
_HANDSHAKE_TIMEOUT_SECONDS = 15.0
_DEFAULT_BACKOFF_MAX_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One interpreted provider message.

    ``kind`` is one of ``status``, ``quote``, ``error``, or ``ignored``.
    """

    kind: str
    status: str | None = None
    message: str | None = None
    quote: Quote | None = None


@dataclass(slots=True)
class _CachedQuote:
    quote: Quote
    received_at: datetime
    sequence: int | None = None


class StreamProtocol:
    """Provider-specific wire format for one streaming feed."""

    name = "protocol"
    provider_id = "none"

    def endpoint(self, settings) -> str:
        raise NotImplementedError

    def headers(self, settings) -> dict[str, str]:
        return {}

    def auth_message(self, settings) -> str | None:
        return None

    def subscribe_message(self, symbols: Sequence[str]) -> str:
        raise NotImplementedError

    def interpret(self, raw: str) -> list[StreamEvent]:
        raise NotImplementedError

    # shared helpers ------------------------------------------------------
    @staticmethod
    def _load(raw: str) -> list | object | None:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    @staticmethod
    def _detail(event: dict) -> str | None:
        for key in ("message", "msg", "error", "detail", "reason", "status_message"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:_MAX_DETAIL]
        return None


class PolygonStreamProtocol(StreamProtocol):
    """Polygon / Massive stocks WebSocket (``T`` trade channel).

    Wire format per https://polygon.io/docs/websocket/stocks/trades:

    * authenticate ``{"action":"auth","params":"<key>"}`` and wait for
      ``{"ev":"status","status":"auth_success"}``;
    * subscribe ``{"action":"subscribe","params":"T.AAPL,T.MSFT"}``;
    * trades arrive as ``{"ev":"T","sym","p","s","t"}`` with ``t`` in epoch ms.

    Only real trades are used as prices. The quote channel is not subscribed
    because a mid-price derived from a book is not an observed trade.
    """

    name = "polygon"
    provider_id = "polygon"
    REALTIME_URL = "wss://socket.massive.com/stocks"
    DELAYED_URL = "wss://delayed.massive.com/stocks"

    def __init__(self, feed: str = "realtime") -> None:
        feed = (feed or "realtime").strip().lower()
        if feed not in ("realtime", "delayed"):
            raise ValueError(
                "MARKET_DATA_STREAM_FEED must be 'realtime' or 'delayed' for Polygon, "
                f"received {feed!r}")
        self.feed = feed

    def endpoint(self, settings) -> str:
        override = getattr(settings, "stream_url", None)
        if override:
            return override.strip()
        return self.REALTIME_URL if self.feed == "realtime" else self.DELAYED_URL

    @property
    def entitlement(self) -> str:
        """What the endpoint implies about data recency - reported, never assumed."""
        return "real-time" if self.feed == "realtime" else "15-minute delayed"

    def auth_message(self, settings) -> str | None:
        key = getattr(settings, "api_key", None)
        if not key:
            return None
        return json.dumps({"action": "auth", "params": key})

    def subscribe_message(self, symbols: Sequence[str]) -> str:
        params = ",".join(f"T.{symbol}" for symbol in symbols)
        return json.dumps({"action": "subscribe", "params": params})

    def interpret(self, raw: str) -> list[StreamEvent]:
        payload = self._load(raw)
        if payload is None:
            return [StreamEvent("ignored", message="the provider sent a non-JSON message")]
        items = payload if isinstance(payload, list) else [payload]
        events: list[StreamEvent] = []
        for item in items:
            if not isinstance(item, dict):
                events.append(StreamEvent("ignored", message="the provider sent a non-object event"))
                continue
            kind = str(item.get("ev") or "").upper()
            if kind == "T":
                events.append(self._trade(item))
            elif kind == "STATUS" or (not kind and item.get("status")):
                events.append(self._status(item))
            else:
                events.append(StreamEvent("ignored",
                                          message=f"unhandled event type {kind or 'unknown'}"))
        return events

    def _trade(self, item: dict) -> StreamEvent:
        try:
            symbol = str(item["sym"]).strip().upper()
            price = float(item["p"])
            volume = float(item.get("s") or 0)
            timestamp = _epoch_ms_to_utc(item["t"])
            quote = normalize_quote(Quote(symbol=symbol, timestamp=timestamp,
                                          price=price, volume=volume))
        except (KeyError, TypeError, ValueError, DataValidationError) as error:
            return StreamEvent("ignored",
                               message=f"dropped an unusable trade event: {error}")
        return StreamEvent("quote", quote=quote)

    def _status(self, item: dict) -> StreamEvent:
        status = str(item.get("status") or "").strip().lower()
        message = self._detail(item) or status or "status update"
        if status in ("auth_success", "authenticated", "connected", "subscribed",
                      "unsubscribed", "success", "ok"):
            return StreamEvent("status", status=status, message=message)
        if "auth" in status or "unauthor" in message.lower() or "api key" in message.lower():
            return StreamEvent("error", status="AUTH_FAILED", message=message)
        if "subscri" in status:
            return StreamEvent("error", status="SUBSCRIPTION_FAILED", message=message)
        if status in ("failed", "error", "fail"):
            return StreamEvent("error", status="ERROR", message=message)
        return StreamEvent("status", status=status or "update", message=message)


class AlpacaStreamProtocol(StreamProtocol):
    """Alpaca stocks market-data WebSocket (``t`` trade channel).

    Wire format per https://docs.alpaca.markets/us/docs/streaming-market-data:

    * authenticate with handshake headers ``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY``
      and read ``{"T":"success","msg":"connected"}`` then ``...authenticated``;
    * subscribe ``{"action":"subscribe","trades":["AAPL"]}``;
    * trades arrive as ``{"T":"t","sym","p","s","t":"<RFC3339>"}``.

    The free plan streams the IEX feed; ``MARKET_DATA_STREAM_FEED`` selects
    ``iex`` (default), ``sip``, or ``delayed_sip``, and the selected feed is
    reported so the dashboard never claims SIP recency it is not entitled to.
    """

    name = "alpaca"
    provider_id = "alpaca"
    BASE_URL = "wss://stream.data.alpaca.markets/v2"
    FEEDS = ("iex", "sip", "delayed_sip")
    FEED_LABELS = {
        "iex": "real-time IEX (free plan)",
        "sip": "real-time SIP consolidated",
        "delayed_sip": "15-minute delayed SIP",
    }

    def __init__(self, feed: str = "iex") -> None:
        feed = (feed or "iex").strip().lower()
        if feed not in self.FEEDS:
            raise ValueError(
                "MARKET_DATA_STREAM_FEED must be one of "
                f"{', '.join(self.FEEDS)} for Alpaca, received {feed!r}")
        self.feed = feed

    def endpoint(self, settings) -> str:
        override = getattr(settings, "stream_url", None)
        if override:
            return override.strip()
        return f"{self.BASE_URL}/{self.feed}"

    @property
    def entitlement(self) -> str:
        return self.FEED_LABELS[self.feed]

    def headers(self, settings) -> dict[str, str]:
        key = (getattr(settings, "api_key", None) or "").strip()
        secret = (getattr(settings, "api_secret", None) or "").strip()
        if not key:
            return {}
        headers = {"APCA-API-KEY-ID": key}
        if secret:
            headers["APCA-API-SECRET-KEY"] = secret
        return headers

    def auth_message(self, settings) -> str | None:
        # Alpaca authenticates in the handshake; no in-band frame is required.
        return None

    def subscribe_message(self, symbols: Sequence[str]) -> str:
        return json.dumps({"action": "subscribe", "trades": list(symbols)})

    def interpret(self, raw: str) -> list[StreamEvent]:
        payload = self._load(raw)
        if payload is None:
            return [StreamEvent("ignored", message="the provider sent a non-JSON message")]
        items = payload if isinstance(payload, list) else [payload]
        events: list[StreamEvent] = []
        for item in items:
            if not isinstance(item, dict):
                events.append(StreamEvent("ignored", message="the provider sent a non-object event"))
                continue
            kind = str(item.get("T") or "").lower()
            if kind == "t":
                events.append(self._trade(item))
            elif kind in ("success", "sub"):
                events.append(StreamEvent("status",
                                          status=str(item.get("msg") or kind).lower(),
                                          message=str(item.get("msg") or kind)))
            elif kind == "subscription":
                subscribed = ", ".join(str(symbol) for symbol in item.get("trades") or [])
                events.append(StreamEvent("status", status="subscribed",
                                          message=f"subscribed to {subscribed or 'no symbols'}"))
            elif kind == "error":
                message = self._detail(item) or "provider error"
                lowered = message.lower()
                code = str(item.get("code") or "")
                if "auth" in lowered or "401" in code or "403" in code or "key" in lowered:
                    return_code = "AUTH_FAILED"
                elif "subscri" in lowered:
                    return_code = "SUBSCRIPTION_FAILED"
                else:
                    return_code = "ERROR"
                events.append(StreamEvent("error", status=return_code, message=message))
            else:
                events.append(StreamEvent("ignored",
                                          message=f"unhandled event type {kind or 'unknown'}"))
        return events

    def _trade(self, item: dict) -> StreamEvent:
        try:
            symbol = str(item["sym"]).strip().upper()
            price = float(item["p"])
            volume = float(item.get("s") or 0)
            quote = normalize_quote(Quote(symbol=symbol,
                                          timestamp=_rfc3339_to_utc(item["t"]),
                                          price=price, volume=volume))
        except (KeyError, TypeError, ValueError, DataValidationError) as error:
            return StreamEvent("ignored",
                               message=f"dropped an unusable trade event: {error}")
        return StreamEvent("quote", quote=quote)


PROTOCOLS: dict[str, type[StreamProtocol]] = {
    "polygon": PolygonStreamProtocol,
    "alpaca": AlpacaStreamProtocol,
}


def protocol_for(provider_id: str, feed: str = "") -> StreamProtocol | None:
    """Build the streaming protocol for a configured provider, or None."""
    found = PROTOCOLS.get((provider_id or "").strip().lower())
    if found is None:
        return None
    return found(feed) if feed else found()


@dataclass(slots=True)
class StreamSnapshot:
    """What the stream is really doing right now (never a claim, always state)."""

    provider: str
    protocol: str
    state: str
    reason: str | None
    entitlement: str | None
    endpoint: str
    connected_at: datetime | None
    authenticated: bool
    subscription: str | None
    symbols: tuple[str, ...]
    last_event_at: datetime | None
    last_event_symbol: str | None
    events_received: int
    quotes_received: int
    ignored_messages: int
    reconnects: int
    attempts: int
    last_error: str | None

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "protocol": self.protocol,
            "state": self.state,
            "reason": self.reason,
            "entitlement": self.entitlement,
            "endpoint": _redact_endpoint(self.endpoint),
            "connected_at": _iso(self.connected_at),
            "authenticated": self.authenticated,
            "subscription": self.subscription,
            "symbols": list(self.symbols),
            "last_event_at": _iso(self.last_event_at),
            "last_event_symbol": self.last_event_symbol,
            "events_received": self.events_received,
            "quotes_received": self.quotes_received,
            "ignored_messages": self.ignored_messages,
            "reconnects": self.reconnects,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "streaming": self.state in STREAMING_STATES,
        }


class MarketDataStream:
    """Maintains one provider WebSocket and a normalized live-quote cache."""

    def __init__(self, *, protocol: StreamProtocol, settings, symbols: Sequence[str],
                 stale_after_seconds: float = 300.0,
                 backoff_max_seconds: float = _DEFAULT_BACKOFF_MAX_SECONDS,
                 clock=None) -> None:
        self._protocol = protocol
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = None
        self._symbols = _normalize_symbols(symbols)
        self._stale_after_seconds = float(stale_after_seconds)
        self._backoff_max = float(backoff_max_seconds)

        self._state = STATE_CONNECTING
        self._reason: str | None = None
        self._connected_at: datetime | None = None
        self._authenticated = False
        self._subscription: str | None = None
        self._last_event_at: datetime | None = None
        self._last_event_symbol: str | None = None
        self._events_received = 0
        self._quotes_received = 0
        self._ignored = 0
        self._reconnects = 0
        self._attempts = 0
        self._last_error: str | None = None
        self._cache: dict[str, _CachedQuote] = {}

    # -- lifecycle --------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="alpha-market-stream",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        client = self._client
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            self._state = STATE_STOPPED
            self._reason = "the market-data stream was stopped"

    # -- reads ------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return StreamSnapshot(
                provider=self._protocol.provider_id,
                protocol=self._protocol.name,
                state=self._state,
                reason=self._reason,
                entitlement=getattr(self._protocol, "entitlement", None),
                endpoint=self._protocol.endpoint(self._settings),
                connected_at=self._connected_at,
                authenticated=self._authenticated,
                subscription=self._subscription,
                symbols=self._symbols,
                last_event_at=self._last_event_at,
                last_event_symbol=self._last_event_symbol,
                events_received=self._events_received,
                quotes_received=self._quotes_received,
                ignored_messages=self._ignored,
                reconnects=self._reconnects,
                attempts=self._attempts,
                last_error=self._last_error,
            ).as_dict()

    def latest(self, symbols: Iterable[str] = ()) -> dict[str, Quote]:
        """Cached live quotes for the requested symbols (empty when none arrived).

        Staleness is *not* applied here: the caller classifies age against the
        configured window so an old but genuine trade is reported STALE rather
        than silently dropped or replaced.
        """
        requested = set(_normalize_symbols(tuple(symbols))) if symbols else set(self._symbols)
        with self._lock:
            return {symbol: cached.quote for symbol, cached in self._cache.items()
                    if symbol in requested}

    @property
    def stale_after_seconds(self) -> float:
        return self._stale_after_seconds

    def set_symbols(self, symbols: Sequence[str]) -> None:
        """Re-subscribe when the watched universe changes (reconnects the socket)."""
        wanted = _normalize_symbols(symbols)
        with self._lock:
            if wanted == self._symbols:
                return
            self._symbols = wanted
        self._reconnect_now()

    # -- worker -----------------------------------------------------------
    def _run(self) -> None:
        """Connect, stream, and restore the socket until asked to stop.

        Each failure handler leaves its own honest state on the snapshot
        (``AUTH_FAILED``, ``SUBSCRIPTION_FAILED``, ``ERROR``, ``RECONNECTING``)
        *before* sleeping, so an operator sees why the stream is quiet rather
        than a generic spinner. Backoff is exponential and jittered.
        """
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            with self._lock:
                self._attempts = attempt
                if attempt > 1:
                    self._reconnects += 1
            if attempt > 1 and not self._backoff(attempt - 1):
                break
            try:
                self._session()
                if self._stop.is_set():
                    break
                self._set_state(STATE_RECONNECTING,
                                "the provider closed the stream connection")
            except _FatalStreamError as error:
                # The provider said "no" in a way retrying cannot fix quickly
                # (bad key, missing entitlement): report it, then retry slowly.
                self._set_state(error.state, error.message)
            except WebSocketError as error:
                # A dropped or refused socket is restored; keep an existing
                # refusal state (AUTH_FAILED / SUBSCRIPTION_FAILED) visible.
                if self._state not in (STATE_AUTH_FAILED, STATE_SUBSCRIPTION_FAILED):
                    self._set_state(STATE_RECONNECTING, _detail(error))
            except Exception as error:  # noqa: BLE001 - the loop must survive
                LOG.warning("market-data stream failed: %s", error)
                self._set_state(STATE_ERROR, _detail(error))
                if self._stop.is_set():
                    break
        with self._lock:
            if self._state != STATE_STOPPED:
                self._state = STATE_STOPPED
                self._reason = "the market-data stream was stopped"

    def _session(self) -> None:
        endpoint = self._protocol.endpoint(self._settings)
        if not endpoint.startswith(("ws://", "wss://")):
            raise _FatalStreamError(STATE_ERROR,
                                    f"the configured stream endpoint is not a ws:// URL")
        with self._lock:
            self._state = STATE_CONNECTING
            self._reason = None
            self._authenticated = False
            self._subscription = None
            self._connected_at = None

        client = WebSocketClient.connect(
            endpoint, timeout=self._handshake_timeout(),
            headers=self._protocol.headers(self._settings))
        self._client = client
        try:
            auth = self._protocol.auth_message(self._settings)
            if auth:
                client.send_text(auth)
                self._apply(self._await(client, self._is_auth, "authentication",
                                        STATE_AUTH_FAILED))
            with self._lock:
                self._authenticated = True
            client.send_text(self._protocol.subscribe_message(self._symbols))
            self._apply(self._await(client, self._is_subscription, "subscription",
                                    STATE_SUBSCRIPTION_FAILED))
            with self._lock:
                self._state = STATE_CONNECTED
                self._connected_at = self._clock()
                self._reason = None
                self._subscription = (
                    f"subscribed to {', '.join(self._symbols)} "
                    f"via {self._protocol.name} {endpoint}")

            while not self._stop.is_set():
                try:
                    raw = client.recv(timeout=_RECV_POLL_SECONDS)
                except TimeoutError:
                    continue
                for event in self._protocol.interpret(raw):
                    self._apply(event)
                if self._state in (STATE_AUTH_FAILED, STATE_SUBSCRIPTION_FAILED):
                    raise _FatalStreamError(self._state, self._reason or "provider refused the stream")
        except TimeoutError as error:
            raise _FatalStreamError(STATE_ERROR,
                                    "the provider did not answer the stream handshake in time") from error
        except WebSocketError as error:
            if self._state in (STATE_AUTH_FAILED, STATE_SUBSCRIPTION_FAILED):
                raise _FatalStreamError(self._state, self._reason) from error
            self._set_state(STATE_RECONNECTING, _detail(error))
            raise
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - already going away
                pass
            self._client = None
            with self._lock:
                self._authenticated = False

    def _await(self, client, predicate, purpose: str, failed_state: str) -> StreamEvent:
        """Read messages until ``predicate(event)`` matches or the deadline passes.

        A provider refusal raises immediately with the state that describes the
        phase that failed (authentication vs subscription), even when the vendor
        only sent a generic ``failed`` status - the handshake is over, and the
        caller must report it instead of continuing to subscribe.
        """
        deadline = time.monotonic() + self._handshake_timeout()
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                raw = client.recv(timeout=_RECV_POLL_SECONDS)
            except TimeoutError:
                continue
            for event in self._protocol.interpret(raw):
                if event.kind == "error":
                    self._apply(event)
                    state = event.status if event.status in (
                        STATE_AUTH_FAILED, STATE_SUBSCRIPTION_FAILED) else failed_state
                    raise _FatalStreamError(
                        state, event.message or "the provider refused the stream")
                if predicate(event):
                    return event
                self._apply(event)
        raise _FatalStreamError(
            STATE_ERROR,
            f"the provider did not confirm {purpose} within "
            f"{self._handshake_timeout():g} seconds")

    def _apply(self, event: StreamEvent) -> None:
        with self._lock:
            self._events_received += 1
            self._last_event_at = self._clock()
            if event.kind == "quote" and event.quote is not None:
                cached = self._cache.get(event.quote.symbol)
                if cached is None or event.quote.timestamp >= cached.quote.timestamp:
                    self._cache[event.quote.symbol] = _CachedQuote(
                        quote=event.quote, received_at=self._clock())
                    self._quotes_received += 1
                    self._last_event_symbol = event.quote.symbol
                return
            if event.kind == "ignored":
                self._ignored += 1
                return
            if event.kind == "error":
                self._last_error = event.message
                self._reason = event.message
                if event.status == "AUTH_FAILED":
                    self._state = STATE_AUTH_FAILED
                elif event.status == "SUBSCRIPTION_FAILED":
                    self._state = STATE_SUBSCRIPTION_FAILED
                elif self._state not in (STATE_AUTH_FAILED, STATE_SUBSCRIPTION_FAILED):
                    self._state = STATE_ERROR
                return
            if event.status in ("auth_success", "authenticated", "connected"):
                self._authenticated = True
                self._reason = None

    # -- helpers ----------------------------------------------------------
    def _set_state(self, state: str, reason: str | None) -> None:
        with self._lock:
            self._state = state
            if reason:
                self._reason = reason[:_MAX_DETAIL]
                if state in (STATE_AUTH_FAILED, STATE_SUBSCRIPTION_FAILED, STATE_ERROR):
                    self._last_error = self._reason

    def _is_auth(self, event: StreamEvent) -> bool:
        return event.kind == "status" and event.status in (
            "auth_success", "authenticated", "connected", "success", "ok")

    def _is_subscription(self, event: StreamEvent) -> bool:
        return event.kind == "status" and event.status in (
            "subscribed", "sub", "success", "ok")

    def _handshake_timeout(self) -> float:
        return _HANDSHAKE_TIMEOUT_SECONDS

    def _backoff(self, attempt: int) -> bool:
        """Sleep with capped exponential backoff. True means "keep going"."""
        delay = min(self._backoff_max, 0.5 * (2 ** min(attempt, 8)))
        delay += random.uniform(0, min(delay * 0.25, 1.0))
        return not self._stop.wait(delay)

    def _reconnect_now(self) -> None:
        client = self._client
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - forcing a reconnect
                pass


class _FatalStreamError(Exception):
    """A provider-level refusal that deserves its own reported state."""

    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state
        self.message = message


def _normalize_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        symbol.strip().upper() for symbol in symbols
        if isinstance(symbol, str) and symbol.strip()))


def _epoch_ms_to_utc(value) -> datetime:
    number = float(value)
    if abs(number) > 1e14:          # nanoseconds
        number /= 1e9
    elif abs(number) > 1e11:        # milliseconds
        number /= 1e3
    moment = datetime.fromtimestamp(number, tz=timezone.utc)
    if not 1990 <= moment.year <= 2100:
        raise ValueError("timestamp is outside a plausible market-data range")
    return moment


def _rfc3339_to_utc(value) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # RFC 3339 allows nanoseconds; datetime parses microseconds.
    head, sep, fraction = text.partition(".")
    if sep:
        tail = fraction
        offset = ""
        for index, char in enumerate(fraction):
            if char in "+-":
                offset = fraction[index:]
                tail = fraction[:index]
                break
        text = f"{head}.{tail[:6]}{offset}"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).isoformat() if moment else None


def _detail(error: Exception) -> str:
    return (str(error) or error.__class__.__name__)[:_MAX_DETAIL]


def _redact_endpoint(endpoint: str) -> str:
    """Never echo a query-string credential from a custom endpoint."""
    if "?" not in endpoint:
        return endpoint
    return endpoint.split("?", 1)[0] + "?***"


# -- construction from configuration ---------------------------------------
STATE_READY = "READY"


def stream_configuration(settings) -> dict:
    """Honest description of what the stream *would* do, with no secrets.

    ``READY`` means every precondition for a real connection exists (provider,
    streaming adapter, credentials) - it is never reported as ``CONNECTED``.
    Anything else carries the exact reason the stream will not start.
    """
    provider = (str(getattr(settings, "provider", "none")) or "none").strip().lower()
    protocol_cls = PROTOCOLS.get(provider)
    if not getattr(settings, "stream", True):
        state, reason = STATE_OFF, REASON_DISABLED
    elif provider in ("", "none"):
        state, reason = STATE_NOT_CONFIGURED, REASON_NO_PROVIDER
    elif protocol_cls is None:
        state, reason = (STATE_NOT_CONFIGURED,
                         f"the {provider} market-data provider has no streaming "
                         "adapter in this build")
    elif not getattr(settings, "configured", bool(getattr(settings, "api_key", None))):
        state, reason = (STATE_NOT_CONFIGURED,
                         str(getattr(settings, "unavailable_reason", None)
                             or REASON_NO_CREDENTIALS))
    else:
        state, reason = STATE_READY, None
    entitlement = None
    feed = (getattr(settings, "stream_feed", "") or "").strip().lower()
    if protocol_cls is not None:
        try:
            entitlement = protocol_cls(feed).entitlement if feed else protocol_cls().entitlement
        except ValueError as error:
            state, reason = STATE_ERROR, str(error)
    return {
        "state": state,
        "reason": reason,
        "provider": provider,
        "protocol": protocol_cls.name if protocol_cls else None,
        "entitlement": entitlement,
        "feed": feed or None,
        "streaming": False,
    }


def build_market_stream(settings, *, symbols: Sequence[str], clock=None,
                        stale_after_seconds: float | None = None):
    """Create the live stream for the configured provider, or ``None``.

    ``None`` is a first-class answer: it means streaming is disabled,
    unconfigured, or unsupported, and :func:`stream_configuration` explains
    which. A provider without credentials never gets a socket.
    """
    if stream_configuration(settings)["state"] != STATE_READY:
        return None
    provider = str(settings.provider).strip().lower()
    feed = (getattr(settings, "stream_feed", "") or "").strip().lower()
    protocol = protocol_for(provider, feed)
    if protocol is None:  # pragma: no cover - guarded by stream_configuration
        return None
    return MarketDataStream(
        protocol=protocol, settings=settings, symbols=symbols,
        stale_after_seconds=(stale_after_seconds if stale_after_seconds is not None
                             else settings.stale_after_seconds),
        clock=clock)
