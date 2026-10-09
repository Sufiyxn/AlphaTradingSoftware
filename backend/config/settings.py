"""Server and trading configuration sections.

All sections are immutable. Secrets live on ``*Settings`` objects but are
excluded from every ``describe()``/``as_dict()`` output so they can never be
serialized into an API response, a log line, or the SQLite journal.
"""
from dataclasses import dataclass

from backend.market_data.session import SESSION_POLICIES
from backend.modes import ModeSelection, TradingMode, parse_mode


DEFAULT_UNIVERSE: tuple[str, ...] = ("AAPL", "BAC", "F", "PLTR", "SOFI")


@dataclass(frozen=True, slots=True)
class Settings:
    """Non-secret server settings consumed by the HTTP layer."""

    host: str = "127.0.0.1"
    port: int = 8000
    environment: str = "development"
    database_path: str = "data/alpha_trading.sqlite3"
    owner_token: str | None = None
    require_api_auth: bool = False
    allow_non_localhost: bool = False

    LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

    @property
    def owner_controls_enabled(self) -> bool:
        return bool(self.owner_token)

    @property
    def loopback(self) -> bool:
        return self.host in self.LOOPBACK_HOSTS

    @property
    def network_exposure(self) -> str:
        return "LOCAL" if self.loopback else "NON_LOCALHOST"

    def describe(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "environment": self.environment,
            "database_path": self.database_path,
            "owner_controls_enabled": self.owner_controls_enabled,
            "require_api_auth": self.require_api_auth,
            "allow_non_localhost": self.allow_non_localhost,
            "network_exposure": self.network_exposure,
        }


@dataclass(frozen=True, slots=True)
class TradingSettings:
    """Runtime trading behaviour: mode intent, watchlist, cadence, price ceiling."""

    mode: TradingMode = TradingMode.PAPER
    live_trading_enabled: bool = False
    universe: tuple[str, ...] = DEFAULT_UNIVERSE
    scan_interval_seconds: float = 30.0
    history_lookback_days: float = 90.0
    max_candidate_price: float = 100.0
    paper_starting_cash: float = 100_000.0
    #: Phase 11: which market sessions accept a *new* order submission.
    #: ``regular`` (default) = the regular session only; ``extended`` also
    #: accepts pre/after-hours when the provider supports extended hours.
    #: Weekends and market holidays are refused under either policy.
    session_submission: str = "regular"

    def __post_init__(self) -> None:
        if self.scan_interval_seconds <= 0:
            raise ValueError("scan_interval_seconds must be positive")
        if self.history_lookback_days <= 0:
            raise ValueError("history_lookback_days must be positive")
        if not 0 < self.max_candidate_price <= 100:
            raise ValueError("max_candidate_price must be in (0, 100]")
        if self.paper_starting_cash <= 0:
            raise ValueError("paper_starting_cash must be positive")
        if not self.universe:
            raise ValueError("at least one watchlist symbol is required")
        if self.session_submission not in SESSION_POLICIES:
            raise ValueError(
                "session_submission must be one of "
                f"{SESSION_POLICIES}, received {self.session_submission!r}")

    @property
    def selection(self) -> ModeSelection:
        return ModeSelection(self.mode, self.live_trading_enabled)

    def describe(self) -> dict:
        return {
            "mode": self.mode.value,
            "live_trading_enabled": self.live_trading_enabled,
            "universe": list(self.universe),
            "scan_interval_seconds": self.scan_interval_seconds,
            "history_lookback_days": self.history_lookback_days,
            "max_candidate_price": self.max_candidate_price,
            "paper_starting_cash": self.paper_starting_cash,
            "session_submission": self.session_submission,
        }


def parse_universe(value: str | None) -> tuple[str, ...]:
    if value is None or not value.strip():
        return DEFAULT_UNIVERSE
    symbols: list[str] = []
    for part in value.replace(";", ",").split(","):
        symbol = part.strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        raise ValueError("UNIVERSE must contain at least one symbol")
    return tuple(symbols)


def parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"expected a boolean value, received {value!r}")


def parse_number(value: str | None, *, default: float, name: str) -> float:
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{name} must be numeric, received {value!r}") from None


__all__ = [
    "DEFAULT_UNIVERSE",
    "ModeSelection",
    "Settings",
    "TradingMode",
    "TradingSettings",
    "parse_bool",
    "parse_mode",
    "parse_number",
    "parse_universe",
]
