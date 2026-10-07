"""Trading execution modes shared by the pipeline, gates, API, and dashboard.

One pipeline serves every mode. The mode only decides where the execution
router sends an approved risk decision:

    BACKTEST -> backtest simulator
    PAPER    -> local paper execution adapter
    LIVE     -> configured broker adapter (only after the live gate opens)

Configuration alone can never grant LIVE. It records intent; the runtime
gate (``backend.execution.gate``) decides whether the intent is satisfiable.
"""
from dataclasses import dataclass
from enum import Enum


class TradingMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class ModeAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class ModeSelection:
    """Mode requested by configuration, before any runtime gate is consulted."""

    mode: TradingMode
    live_trading_enabled: bool = False

    @property
    def configured_mode(self) -> TradingMode:
        return self.mode

    @property
    def live_intent(self) -> bool:
        return self.mode is TradingMode.LIVE and self.live_trading_enabled

    @property
    def effective(self) -> TradingMode:
        """Best mode reachable from configuration alone, ignoring runtime checks."""
        if self.live_intent:
            return TradingMode.LIVE
        if self.mode is TradingMode.LIVE:
            return TradingMode.PAPER
        return self.mode


@dataclass(frozen=True, slots=True)
class ModeStatus:
    """Resolved mode plus the reasons it could not be the configured one."""

    mode: TradingMode
    configured: TradingMode
    availability: ModeAvailability
    blockers: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.availability is ModeAvailability.AVAILABLE

    @property
    def degraded(self) -> bool:
        return self.mode is not self.configured

    def as_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "configured": self.configured.value,
            "availability": self.availability.value,
            "available": self.available,
            "degraded": self.degraded,
            "blockers": list(self.blockers),
        }


def parse_mode(value: str | None) -> TradingMode:
    normalized = (value or TradingMode.PAPER.value).strip().upper()
    try:
        return TradingMode(normalized)
    except ValueError:
        raise ValueError(
            f"unsupported trading mode {normalized!r}; expected one of "
            f"{', '.join(item.value for item in TradingMode)}"
        ) from None
