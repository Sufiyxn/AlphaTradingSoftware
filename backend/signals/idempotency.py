"""Deterministic duplicate and replay protection for inbound signals.

Every accepted or risk-decided signal is remembered under deterministic keys:
the caller-supplied event id when the provider supplies one, plus the SHA-256
digest of the exact request body. Records are persisted in the journal as
``SIGNAL_PROCESSED`` events, so a process restart cannot turn a replay into a
second paper order. Tracked keys are bounded; the signal freshness window
guarantees an evicted key can no longer be validated anyway.
"""
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Any, Iterable, Mapping

PROCESSED_EVENT = "SIGNAL_PROCESSED"


class SignalReplayStore:
    """Thread-safe, journal-backed idempotency keys for signal processing."""

    def __init__(self, repository=None, *, max_tracked: int = 10_000, clock=None):
        if max_tracked < 1:
            raise ValueError("max_tracked must be positive")
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._max_tracked = max_tracked
        self._repository = repository
        self._clock = clock
        self._hydrate()

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)

    def seen(self, keys: Iterable[str]) -> bool:
        with self._lock:
            return any(key in self._seen for key in keys if key)

    def record_for(self, keys: Iterable[str]) -> dict[str, Any] | None:
        """Return the stored processing record when any key was already seen."""
        with self._lock:
            for key in keys:
                if key:
                    record = self._seen.get(key)
                    if record is not None:
                        return record
        return None

    def remember(self, keys: Iterable[str], *, signal_id: str, source: str,
                 outcome: str, symbol: str | None = None,
                 timestamp: datetime | None = None,
                 context: Mapping[str, Any] | None = None) -> None:
        live_keys = tuple(str(key) for key in keys if key)
        if not live_keys:
            return
        record = {
            "signal_id": signal_id,
            "source": source,
            "outcome": outcome,
            "symbol": symbol,
            "keys": list(live_keys),
        }
        with self._lock:
            for key in live_keys:
                self._seen[key] = record
                self._seen.move_to_end(key)
            while len(self._seen) > self._max_tracked:
                self._seen.popitem(last=False)
        if self._repository is not None:
            recorded_at = timestamp or (self._clock() if self._clock else None)
            if recorded_at is None:
                return
            payload = dict(record)
            if context:
                payload.update({str(key): value for key, value in context.items()})
            try:
                self._repository.record_event(
                    recorded_at, PROCESSED_EVENT,
                    f"processed {source} signal {signal_id}",
                    symbol=symbol, context=payload)
            except Exception:
                # Never lose a safety decision because the journal failed; the
                # in-memory record still blocks a concurrent duplicate.
                pass

    def _hydrate(self) -> None:
        if self._repository is None:
            return
        try:
            events = self._repository.events(limit=None)
        except Exception:
            return
        # Events arrive newest first; replay them oldest first so the bounded
        # window trims stale keys and the newest record for a key wins.
        for event in reversed(events):
            if event.get("event_type") != PROCESSED_EVENT:
                continue
            context = event.get("context_json") or {}
            if not isinstance(context, Mapping):
                continue
            keys = context.get("keys") or []
            record = {
                "signal_id": context.get("signal_id"),
                "source": context.get("source"),
                "outcome": context.get("outcome"),
                "symbol": context.get("symbol"),
                "keys": list(keys),
                "hydrated": True,
            }
            for key in keys:
                if key:
                    self._seen[str(key)] = record
                    self._seen.move_to_end(str(key))
        while len(self._seen) > self._max_tracked:
            self._seen.popitem(last=False)
