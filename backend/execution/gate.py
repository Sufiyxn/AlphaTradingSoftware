"""Paper/live execution authorization - the execution safety gate.

The gate is the single place that decides whether an execution mode may be
used. Credentials, configuration, and provider identity never grant LIVE:
LIVE stays blocked until this build ships a verified live execution adapter
plus the explicit live-safety controls of a later phase.

The gate reads the RiskEngine kill switch through an injected callable and
also carries its own manual emergency latch. It never resets the risk switch,
and engaging either stop blocks every execution mode.
"""
from __future__ import annotations

from typing import Callable

from .models import ExecutionMode

LIVE_BLOCKERS: tuple[str, ...] = (
    "no verified live execution connection exists",
    "no live execution adapter is installed in this build",
    "live execution requires explicit safety gates that are not implemented yet",
)


class ExecutionSafetyGate:
    """Authorizes execution modes; fail-closed for LIVE."""

    def __init__(self, *, kill_switch: Callable[[], bool] | None = None,
                 live_enabled: Callable[[], bool] | None = None) -> None:
        self._kill_switch = kill_switch or (lambda: False)
        self._live_enabled = live_enabled or (lambda: False)
        self._manual_engaged = False
        self._manual_reason: str | None = None

    def engage(self, reason: str) -> None:
        """Manual emergency stop for the execution path."""
        text = str(reason or "").strip()
        self._manual_engaged = True
        self._manual_reason = text or "owner engaged the execution stop"

    def release(self, reason: str) -> None:
        """Release the manual latch. The risk kill switch is never touched."""
        self._manual_engaged = False
        self._manual_reason = None

    @property
    def manual_engaged(self) -> bool:
        return self._manual_engaged

    @property
    def kill_switch_engaged(self) -> bool:
        return self._manual_engaged or bool(self._kill_switch())

    @property
    def kill_switch_reason(self) -> str | None:
        if self._manual_engaged:
            return f"the execution stop is engaged ({self._manual_reason})"
        if self._kill_switch():
            return "the risk emergency stop is engaged"
        return None

    def blockers(self, mode: ExecutionMode) -> tuple[str, ...]:
        reason = self.kill_switch_reason
        if reason:
            return (reason,)
        if mode is ExecutionMode.PAPER:
            return ()
        live = list(LIVE_BLOCKERS)
        if not self._live_enabled():
            live.insert(0, "live trading intent is not enabled in the configuration")
        return tuple(live)

    def authorize(self, mode: ExecutionMode) -> tuple[bool, str]:
        blockers = self.blockers(mode)
        if not blockers:
            return True, "paper execution is authorized"
        return False, "; ".join(blockers)

    def describe(self) -> dict:
        paper_allowed, _ = self.authorize(ExecutionMode.PAPER)
        live_allowed, live_reason = self.authorize(ExecutionMode.LIVE)
        return {
            "role": "EXECUTION_SAFETY_GATE",
            "paper_allowed": paper_allowed,
            "live_allowed": live_allowed,
            "live_reason": None if live_allowed else live_reason,
            "kill_switch_engaged": self.kill_switch_engaged,
            "kill_switch_reason": self.kill_switch_reason,
            "manual_stop_engaged": self._manual_engaged,
        }
