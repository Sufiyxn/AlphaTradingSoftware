"""Shared scaffolding for real vendor adapters.

One HTTP/JSON path, one health record, one credential-redaction rule for every
adapter. Adapters implement ``_fetch_latest``, ``_fetch_historical``,
``_body_outcome``, and ``_probe``; everything else here guarantees that a
failure surfaces as an explicit error state instead of fabricated data.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .health import HealthTracker, utc_now
from .http import HttpTransport, UrllibTransport, with_params
from .models import Bar, Quote
from .provider import (
    MarketDataProviderError,
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderNoDataError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

USER_AGENT = "AlphaTradingSoftware/1.0 (read-only market data)"
_MAX_DETAIL = 300

_OUTCOME_OK = "ok"
_OUTCOME_AUTH = "auth"
_OUTCOME_RATE = "rate"
_OUTCOME_ERROR = "error"


class VendorMarketDataProvider:
    """Transport, classification, and health for one vendor API."""

    provider: str = "vendor"
    label: str = "Provider"
    capabilities: tuple[str, ...] = ()
    default_base_url: str = ""

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 transport: HttpTransport | None = None, clock=None,
                 timeout_seconds: float = 10.0,
                 stale_after_seconds: float = 300.0, timeframe: str = "1min") -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError(f"the {self.label} API key is required")
        self._api_key = api_key.strip()
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        if not self.base_url:
            raise ValueError(f"a base URL is required for {self.label}")
        self.timeframe = timeframe
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("the market data timeout must be positive")
        self._transport = transport if transport is not None else UrllibTransport()
        self._clock = clock or utc_now
        self._health = HealthTracker(self.provider, clock=self._clock,
                                     capabilities=self.capabilities,
                                     stale_after_seconds=stale_after_seconds)
        self.is_configured = True

    @property
    def symbols(self) -> tuple[str, ...]:
        return ()

    @property
    def health(self) -> dict:
        """Current connectivity snapshot; stale success downgrades to STALE."""
        return self._health.snapshot()

    def health_check(self) -> dict:
        """Backend connectivity probe; never invoked from the browser."""
        try:
            detail = self._probe()
        except ProviderRateLimitError as error:
            return {"success": False, "status": "RATE_LIMITED", "detail": str(error)}
        except ProviderNoDataError:
            return {"success": True, "status": "PASSED",
                    "detail": f"the {self.label} API answered the connectivity check"}
        except NotImplementedError:
            return {"success": False, "status": "UNSUPPORTED",
                    "detail": f"the {self.label} provider exposes no connectivity probe; "
                              "no network request was made"}
        except MarketDataProviderError as error:
            return {"success": False, "status": "FAILED", "detail": str(error)}
        return {"success": True, "status": "PASSED", "detail": detail}

    def get_latest(self, symbols: Sequence[str]) -> Sequence[Quote]:
        raise NotImplementedError

    def get_historical(self, symbol: str, start: datetime, end: datetime) -> Sequence[Bar]:
        raise NotImplementedError

    def _probe(self) -> str:
        raise NotImplementedError

    def _extra_headers(self) -> Mapping[str, str]:
        """Headers every request carries (e.g. header-authenticated vendors)."""
        return {}

    def _fetch_latest(self, symbols: Sequence[str]) -> Sequence[Quote]:
        raise NotImplementedError

    def _fetch_historical(self, symbol: str, start: datetime,
                          end: datetime) -> Sequence[Bar]:
        raise NotImplementedError

    def _body_outcome(self, payload: Any, purpose: str) -> tuple[str, str]:
        """Map a parsed body to an outcome; override for vendor-specific fields."""
        if isinstance(payload, dict):
            if not payload:
                return _OUTCOME_ERROR, "the response body was an empty object"
            return _OUTCOME_OK, ""
        if isinstance(payload, list):
            return _OUTCOME_OK, ""
        return _OUTCOME_ERROR, "the response body was not a JSON object"

    def _request_json(self, url: str, *, params: Mapping[str, object] | None = None,
                      purpose: str, missing_status_is_empty: bool = False) -> Any:
        """GET one URL, classify the answer, and record real connectivity."""
        full_url = with_params(url, params)
        self._health.connecting(purpose)
        try:
            response = self._transport.get(
                full_url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT,
                         **dict(self._extra_headers())},
                timeout=self.timeout_seconds,
            )
        except ProviderTimeoutError:
            message = (f"the {self.label} request timed out after "
                       f"{self.timeout_seconds:g} seconds")
            self._health.unreachable(message)
            raise ProviderTimeoutError(message) from None
        except ProviderConnectionError:
            message = f"the {self.label} service could not be reached; no response was received"
            self._health.unreachable(message)
            raise ProviderConnectionError(message) from None
        except MarketDataProviderError as error:
            message = f"the {self.label} request failed before a response was received"
            self._health.unreachable(message)
            raise ProviderConnectionError(message) from error

        status = int(response.status)
        body_text = response.body.decode("utf-8", "replace") if response.body else ""
        redacted = self._redact(body_text)

        if status in (401, 403):
            detail = self._detail(redacted) or "the configured credentials were rejected"
            message = f"authentication failed: {detail}"
            self._health.failed(message)
            raise ProviderAuthenticationError(message)
        if status == 429:
            detail = self._detail(redacted) or "the request quota was exhausted"
            message = f"rate limited: {detail}"
            until = self._rate_deadline(response.headers, redacted)
            self._health.rate_limited(message, until)
            raise ProviderRateLimitError(message)
        if status == 404 and missing_status_is_empty:
            self._health.connected(f"the {self.label} API answered the {purpose} request")
            raise ProviderNoDataError(
                f"the {self.label} API holds no data for this {purpose}")
        if status >= 500:
            message = f"the {self.label} API returned HTTP {status}; the service is unavailable"
            self._health.unreachable(message)
            raise ProviderUnavailableError(message)
        if status >= 400:
            detail = self._detail(redacted) or "the request was rejected"
            message = f"request rejected (HTTP {status}): {detail}"
            self._health.failed(message)
            raise ProviderResponseError(message)
        if not body_text.strip():
            message = f"the {self.label} API returned an empty response"
            self._health.failed(message)
            raise ProviderResponseError(message)
        try:
            payload = json.loads(body_text)
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = f"the {self.label} API returned a malformed response that is not valid JSON"
            self._health.failed(message)
            raise ProviderResponseError(message) from None

        outcome, detail = self._body_outcome(payload, purpose)
        message = self._redact(detail or "").strip()
        if outcome == _OUTCOME_OK:
            self._health.connected(f"the {self.label} API answered the {purpose} request")
            return payload
        if outcome == "no_data":
            self._health.connected(f"the {self.label} API answered the {purpose} request")
            raise ProviderNoDataError(
                f"the {self.label} API holds no data for this {purpose}")
        if outcome == _OUTCOME_AUTH:
            text = f"authentication failed: {message or 'the credentials were rejected'}"
            self._health.failed(text)
            raise ProviderAuthenticationError(text)
        if outcome == _OUTCOME_RATE:
            text = f"rate limited: {message or 'the request quota was exhausted'}"
            until = self._rate_deadline(response.headers, message)
            self._health.rate_limited(text, until)
            raise ProviderRateLimitError(text)
        text = f"unusable response: {message or 'the response did not match the documented schema'}"
        self._health.failed(text)
        raise ProviderResponseError(text)

    def _malformed(self, detail: str) -> ProviderResponseError:
        """Record and raise a schema failure found after the HTTP layer."""
        text = f"unusable response: {self._redact(detail)}"
        self._health.failed(text)
        return ProviderResponseError(text)

    def _redact(self, text: str) -> str:
        if not text:
            return ""
        for secret in (self._api_key, getattr(self, "_api_secret", None)):
            if secret and len(secret) >= 6 and secret in text:
                text = text.replace(secret, "***")
        if "apiKey=" in text or "api_key=" in text or "apikey=" in text.lower():
            parts = text.split()
            text = " ".join(_strip_query_secret(part) for part in parts)
        return text[:_MAX_DETAIL * 4]

    @staticmethod
    def _detail(body_text: str) -> str | None:
        text = body_text.strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return text[:_MAX_DETAIL]
        if isinstance(payload, dict):
            for field in ("error", "message", "Error Message", "status_message",
                          "detail", "Note", "Information"):
                value = payload.get(field)
                if isinstance(value, str) and value.strip():
                    return value[:_MAX_DETAIL]
            if isinstance(payload.get("status"), str):
                return f"the API reported status {payload['status']}"
            return None
        return text[:_MAX_DETAIL]

    def _rate_deadline(self, headers: Mapping[str, str], body_text: str) -> datetime | None:
        raw = _header(headers, "retry-after")
        if raw:
            try:
                return self._clock() + timedelta(seconds=max(int(raw.strip()), 1))
            except (ValueError, TypeError):
                pass
        lowered = body_text.lower()
        if "per minute" in lowered or "per 1 minute" in lowered or "5 calls" in lowered:
            return self._clock() + timedelta(seconds=60)
        if "per hour" in lowered:
            return self._clock() + timedelta(seconds=3600)
        if "per day" in lowered:
            return self._clock() + timedelta(seconds=86400)
        return None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return str(value)
    return None


def _strip_query_secret(fragment: str) -> str:
    lowered = fragment.lower()
    for marker in ("apikey=", "api_key=", "token=", "key="):
        index = lowered.find(marker)
        if index >= 0:
            head = fragment[:index + len(marker)]
            tail = fragment[index + len(marker):]
            end = len(tail)
            for stop in ("&", '"', "'", ")", ",", " "):
                position = tail.find(stop)
                if position >= 0:
                    end = min(end, position)
            return head + "***" + tail[end:]
    return fragment


def epoch_to_utc(value: Any) -> datetime:
    """Convert an epoch number in seconds/milliseconds/nanoseconds to UTC."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timestamp must be a number")
    magnitude = abs(float(value))
    if magnitude > 1e14:
        seconds = float(value) / 1e9
    elif magnitude > 1e11:
        seconds = float(value) / 1e3
    else:
        seconds = float(value)
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    if not 1990 <= moment.year <= 2100:
        raise ValueError("timestamp is outside a plausible market-data range")
    return moment


def rfc3339_to_utc(value: Any) -> datetime:
    """Parse an RFC 3339 timestamp (including nanoseconds) into UTC."""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    head, sep, fraction = text.partition(".")
    if sep:
        tail, offset = fraction, ""
        for index, char in enumerate(fraction):
            if char in "+-":
                offset = fraction[index:]
                tail = fraction[:index]
                break
        text = f"{head}.{tail[:6]}{offset}"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if not 1990 <= moment.year <= 2100:
        raise ValueError("timestamp is outside a plausible market-data range")
    return moment.astimezone(timezone.utc)


def require_number(value: Any, field: str) -> float:
    """Parse a provider number field, failing closed on junk."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise ValueError(f"{field} is not numeric") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} is not numeric")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{field} is not finite")
    return number
