"""Scripted HTTP transport for market-data adapter tests.

Tests-only: this module is never imported by the production service graph, so
no adapter can accidentally run against canned data in a real process. Queue
responses or route by URL fragment; unexpected requests fail the test loudly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from backend.market_data.http import HttpResponse
from backend.market_data.provider import ProviderConnectionError, ProviderTimeoutError


@dataclass(frozen=True, slots=True)
class FakeResponse:
    """One scripted answer. ``payload`` is JSON-encoded unless it is bytes."""

    status: int = 200
    payload: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)
    raw_body: bytes | None = None

    def to_http(self) -> HttpResponse:
        if self.raw_body is not None:
            body = self.raw_body
        elif isinstance(self.payload, bytes):
            body = self.payload
        elif isinstance(self.payload, str):
            body = self.payload.encode("utf-8")
        else:
            body = json.dumps(self.payload).encode("utf-8")
        headers = {str(key).lower(): str(value) for key, value in self.headers.items()}
        return HttpResponse(self.status, headers, body)


class FakeTransport:
    """HttpTransport double: scripted queue first, then URL-fragment routes."""

    def __init__(self, *responses: Any, routes: Mapping[str, Any] | None = None) -> None:
        self._queue: list[Any] = list(responses)
        self._routes: dict[str, Any] = dict(routes or {})
        self.requests: list[HttpResponse] = []
        self.requested_urls: list[str] = []
        self.requested_timeouts: list[float] = []
        self.requested_headers: list[dict[str, str]] = []

    def enqueue(self, response: Any) -> "FakeTransport":
        self._queue.append(response)
        return self

    def get(self, url: str, *, headers: Mapping[str, str] | None = None,
            timeout: float) -> HttpResponse:
        self.requested_urls.append(url)
        self.requested_timeouts.append(float(timeout))
        self.requested_headers.append({str(key).lower(): str(value)
                                       for key, value in (headers or {}).items()})
        if self._queue:
            item = self._queue.pop(0)
        else:
            item = next((value for fragment, value in self._routes.items()
                         if fragment in url), None)
        if item is None:
            raise AssertionError(f"unexpected market-data request: {url}")
        if callable(item) and not isinstance(item, Exception):
            item = item(url)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, FakeResponse):
            response = item.to_http()
        elif isinstance(item, HttpResponse):
            response = item
        else:
            raise AssertionError(f"unsupported scripted response: {item!r}")
        self.requests.append(response)
        return response


def timeout_error() -> ProviderTimeoutError:
    return ProviderTimeoutError("the provider request timed out after 1 seconds")


def connection_error() -> ProviderConnectionError:
    return ProviderConnectionError("the provider could not be reached; no response was received")
