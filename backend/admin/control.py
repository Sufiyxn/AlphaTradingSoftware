import threading
from datetime import timedelta
from typing import Callable

from backend.database import JournalRepository
from backend.orchestrator import TradingOrchestrator

from .auth import OwnerAuthenticator


class OwnerControlPlane:
    """Authenticated local owner operations with an auditable, constrained surface."""

    def __init__(self, authenticator: OwnerAuthenticator, orchestrator: TradingOrchestrator,
                 repository: JournalRepository, *, shutdown: Callable[[], None] | None = None):
        self.authenticator = authenticator
        self.orchestrator = orchestrator
        self.repository = repository
        self._shutdown = shutdown
        self._worker: threading.Thread | None = None
        self._termination = threading.Event()
        self._lock = threading.Lock()

    @property
    def termination_requested(self) -> bool:
        return self._termination.is_set()

    def wait_for_termination(self, timeout: float | None = None) -> bool:
        return self._termination.wait(timeout)

    def inspect_status(self, credential: str | None) -> dict:
        actor = self._authorize(credential)
        snapshot = self.orchestrator.state
        self._audit("OWNER_STATUS_INSPECTED", actor)
        return {"actor": actor, "orchestrator": snapshot,
                "portfolio": self.orchestrator.portfolio.snapshot(),
                "config": self.orchestrator.config}

    def inspect_recent_logs(self, credential: str | None, limit: int = 50) -> dict:
        actor = self._authorize(credential)
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        return {"actor": actor, "events": self.repository.events(limit=limit),
                "errors": self.repository.errors(limit=limit)}

    def pause(self, credential: str | None) -> None:
        actor = self._authorize(credential)
        self.orchestrator.pause()
        self._audit("OWNER_PAUSED_BOT", actor)

    def resume(self, credential: str | None) -> None:
        actor = self._authorize(credential)
        self.orchestrator.resume()
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self.orchestrator.start,
                                                name="alpha-orchestrator", daemon=True)
                self._worker.start()
        self._audit("OWNER_RESUMED_BOT", actor)

    def update_runtime_config(self, credential: str | None, key: str, value: object) -> None:
        actor = self._authorize(credential)
        if key == "scan_interval_seconds":
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError("scan interval must be positive")
            self.orchestrator.update_runtime_config(scan_interval=timedelta(seconds=float(value)))
        elif key == "history_lookback_days":
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError("history lookback must be positive")
            self.orchestrator.update_runtime_config(history_lookback=timedelta(days=float(value)))
        elif key == "max_candidate_price":
            if not isinstance(value, (int, float)) or not 0 < value <= 100:
                raise ValueError("candidate price limit must be positive and cannot exceed $100")
            self.orchestrator.update_runtime_config(max_candidate_price=float(value))
        else:
            raise ValueError(f"runtime setting is not owner-editable: {key}")
        self._audit("OWNER_UPDATED_RUNTIME_CONFIG", actor, {"key": key, "value": value})

    def emergency_stop(self, credential: str | None, reason: str = "owner emergency stop") -> None:
        actor = self._authorize(credential)
        self.orchestrator.emergency_stop(triggered_by=actor, reason=reason)
        self._audit("OWNER_EMERGENCY_STOP", actor, {"reason": reason})

    def terminate(self, credential: str | None) -> None:
        actor = self._authorize(credential)
        self.orchestrator.terminate(triggered_by=actor)
        self._audit("OWNER_REQUESTED_TERMINATION", actor)
        self._termination.set()
        if self._shutdown:
            self._shutdown()

    def _authorize(self, credential: str | None) -> str:
        actor = self.authenticator.authenticate(credential)
        if actor is None:
            raise PermissionError("owner authentication failed")
        return actor

    def _audit(self, event: str, actor: str, context: dict | None = None) -> None:
        self.repository.record_event(self.orchestrator._clock(), event, event,
                                     context={"actor": actor, "triggered_by": actor, **(context or {})})
