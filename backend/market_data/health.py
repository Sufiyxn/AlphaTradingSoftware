"""Real provider connectivity states.

States are evidence-driven: credentials alone never produce ``CONNECTED``.
``CONNECTED`` is recorded only after a request actually succeeded; a
configured provider starts at ``UNAVAILABLE`` ("no successful check yet")
and is surfaced as ``CONNECTING`` while a request is in flight.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable, Sequence

CONNECTING = "CONNECTING"
CONNECTED = "CONNECTED"
UNAVAILABLE = "UNAVAILABLE"
ERROR = "ERROR"
STALE = "STALE"
RATE_LIMITED = "RATE_LIMITED"

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class HealthTracker:
    """Mutable, thread-safe connectivity record for one provider adapter."""

    def __init__(self, provider: str, *, clock: Clock | None = None,
                 capabilities: Sequence[str] = (),
                 stale_after_seconds: float = 300.0) -> None:
        self.provider = provider
        self.capabilities = tuple(capabilities)
        self.stale_after_seconds = float(stale_after_seconds)
        self._clock = clock or utc_now
        self._lock = threading.Lock()
        self._state = UNAVAILABLE
        self._reason = "the provider has not completed a connectivity check yet"
        self._checked_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error_at: datetime | None = None
        self._last_error: str | None = None
        self._rate_limited_until: datetime | None = None

    def connecting(self, purpose: str) -> None:
        with self._lock:
            self._state = CONNECTING
            self._reason = f"{purpose} in progress"

    def connected(self, detail: str) -> None:
        now = self._clock()
        with self._lock:
            self._state = CONNECTED
            self._reason = None
            self._checked_at = now
            self._last_success_at = now
            self._last_error = None
            self._last_error_at = None
            self._rate_limited_until = None

    def rate_limited(self, detail: str, until: datetime | None) -> None:
        now = self._clock()
        with self._lock:
            self._state = RATE_LIMITED
            self._reason = detail
            self._checked_at = now
            self._last_error = detail
            self._last_error_at = now
            self._rate_limited_until = until

    def failed(self, detail: str) -> None:
        now = self._clock()
        with self._lock:
            self._state = ERROR
            self._reason = detail
            self._checked_at = now
            self._last_error = detail
            self._last_error_at = now
            self._rate_limited_until = None

    def unreachable(self, detail: str) -> None:
        now = self._clock()
        with self._lock:
            self._state = UNAVAILABLE
            self._reason = detail
            self._checked_at = now
            self._last_error = detail
            self._last_error_at = now
            self._rate_limited_until = None

    def snapshot(self) -> dict:
        """Return the current state, downgrading expired evidence."""
        now = self._clock()
        with self._lock:
            state = self._state
            reason = self._reason
            if state == CONNECTED and self._last_success_at is not None:
                age = (now - self._last_success_at).total_seconds()
                if age > self.stale_after_seconds:
                    state = STALE
                    reason = ("the last successful request is older than "
                              f"{self.stale_after_seconds:g} seconds")
            if state == RATE_LIMITED and self._rate_limited_until is not None:
                if now >= self._rate_limited_until:
                    state = UNAVAILABLE
                    reason = ("the previous rate limit window has expired; "
                              "no request has succeeded since")
            return {
                "provider": self.provider,
                "state": state,
                "reason": reason,
                "checked_at": self._checked_at,
                "last_success_at": self._last_success_at,
                "last_error_at": self._last_error_at,
                "last_error": self._last_error,
                "rate_limited_until": self._rate_limited_until,
                "capabilities": list(self.capabilities),
            }
