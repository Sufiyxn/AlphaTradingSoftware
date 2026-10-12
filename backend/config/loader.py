"""Layered configuration loading: process env <- .env file <- documented defaults.

Secrets are read here and only ever stored on immutable settings objects.
``describe()`` on any section reports credential presence, never a value.

``load_config`` deliberately normalises legacy development values so a stale
local ``.env`` cannot make the product pretend an integration is connected:
``MARKET_DATA_PROVIDER=MOCK`` and ``TRADING_PLATFORM_ADAPTER=mock`` both
resolve to "none" (not configured). Deterministic mock fixtures remain
reachable only through ``backend.testing`` helpers used by the test suite.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from backend.market_data.session import normalize_session_policy
from backend.modes import ModeSelection, TradingMode, parse_mode
from backend.risk.config import RiskConfig
from backend.strategies.models import StrategyConfig

from .providers import (
    AISettings,
    BrokerSettings,
    MarketDataSettings,
    NotificationChannelSettings,
    NotificationSettings,
)
from .settings import Settings, TradingSettings, parse_bool, parse_number, parse_universe


_DEV_ONLY_PROVIDERS = {"MOCK", "TEST", "PAPER", "LOCAL", "FIXTURE"}


@dataclass(frozen=True, slots=True)
class Config:
    """Complete typed configuration for one application runtime."""

    app: Settings
    trading: TradingSettings
    strategy: StrategyConfig
    risk: RiskConfig
    market_data: MarketDataSettings
    broker: BrokerSettings
    ai: AISettings
    notifications: NotificationSettings

    @property
    def mode_selection(self) -> ModeSelection:
        return self.trading.selection

    def describe(self) -> dict:
        """Fully redacted configuration view, safe to serialize."""
        return {
            "app": self.app.describe(),
            "trading": self.trading.describe(),
            "mode": self.mode_selection.effective.value,
            "market_data": self.market_data.describe(),
            "broker": self.broker.describe(),
            "ai": self.ai.describe(),
            "notifications": self.notifications.describe(),
            "risk": {
                "enabled": self.risk.enabled,
                "min_reward_risk": self.risk.min_reward_risk,
                "risk_fraction": self.risk.risk_fraction,
                "max_risk_per_trade": self.risk.max_risk_per_trade,
                "max_risk_amount": self.risk.max_risk_amount,
                "max_position_notional": self.risk.max_position_notional,
                "max_portfolio_exposure_fraction": self.risk.max_portfolio_exposure_fraction,
                "max_simultaneous_positions": self.risk.max_simultaneous_positions,
                "max_daily_loss_fraction": self.risk.max_daily_loss_fraction,
                "allow_duplicate_symbol": self.risk.allow_duplicate_symbol,
                "max_quantity": self.risk.max_quantity,
                "max_symbol_exposure": self.risk.max_symbol_exposure,
                "max_entry_price": self.risk.max_entry_price,
                "require_fresh_valuation": self.risk.require_fresh_valuation,
            },
            "strategy": {
                "name": self.strategy_name,
                "fast_ema_period": self.strategy.fast_ema_period,
                "slow_ema_period": self.strategy.slow_ema_period,
                "rsi_period": self.strategy.rsi_period,
                "atr_period": self.strategy.atr_period,
                "atr_multiplier": self.strategy.atr_multiplier,
                "min_reward_risk": self.strategy.min_reward_risk,
            },
        }

    @property
    def strategy_name(self) -> str:
        return "baseline_trend_momentum_v1"


def load_config(env: Mapping[str, str] | None = None) -> Config:
    if env is None:
        load_local_env()
        env = os.environ
    trading = _trading_settings(env)
    return Config(
        app=_app_settings(env),
        trading=trading,
        strategy=_strategy_config(env),
        risk=_risk_config(env, max_candidate_price=trading.max_candidate_price),
        market_data=_market_data_settings(env),
        broker=_broker_settings(env),
        ai=_ai_settings(env),
        notifications=_notification_settings(env),
    )


def load_settings() -> Settings:
    """Backwards-compatible server settings loader."""
    return load_config().app


def load_local_env(path: str | Path = ".env") -> None:
    """Read simple KEY=value entries without overriding the process environment."""
    target = Path(path)
    if not target.is_file():
        return
    for line in target.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        key, value = key.strip(), value.strip().strip("\"'")
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


def _get(env: Mapping[str, str], key: str, default: str | None = None) -> str | None:
    value = env.get(key, default)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _app_settings(env: Mapping[str, str]) -> Settings:
    raw_port = _get(env, "APP_PORT", "8000")
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        raise ValueError(f"APP_PORT must be an integer, received {raw_port!r}") from None
    if not 0 < port < 65536:
        raise ValueError(f"APP_PORT must be between 1 and 65535, received {port}")
    host = _get(env, "APP_HOST", "127.0.0.1")
    allow_non_localhost = parse_bool(_get(env, "ALLOW_NON_LOCALHOST"), default=False)
    return Settings(
        host=host,
        port=port,
        environment=_get(env, "APP_ENV", "development"),
        database_path=_get(env, "DATABASE_PATH", "data/alpha_trading.sqlite3"),
        owner_token=_get(env, "OWNER_CONTROL_TOKEN"),
        require_api_auth=parse_bool(_get(env, "REQUIRE_API_AUTH"), default=False),
        allow_non_localhost=allow_non_localhost,
    )


def _trading_settings(env: Mapping[str, str]) -> TradingSettings:
    return TradingSettings(
        mode=parse_mode(_get(env, "TRADING_MODE")),
        live_trading_enabled=parse_bool(_get(env, "LIVE_TRADING_ENABLED"), default=False),
        universe=parse_universe(_get(env, "UNIVERSE")),
        scan_interval_seconds=parse_number(
            _get(env, "SCAN_INTERVAL_SECONDS"), default=30.0, name="SCAN_INTERVAL_SECONDS"),
        history_lookback_days=parse_number(
            _get(env, "HISTORY_LOOKBACK_DAYS"), default=90.0, name="HISTORY_LOOKBACK_DAYS"),
        max_candidate_price=parse_number(
            _get(env, "MAX_CANDIDATE_PRICE"), default=100.0, name="MAX_CANDIDATE_PRICE"),
        paper_starting_cash=parse_number(
            _get(env, "PAPER_STARTING_CASH"), default=100_000.0, name="PAPER_STARTING_CASH"),
        session_submission=normalize_session_policy(
            _get(env, "MARKET_SESSION_SUBMISSION")),
    )


def _strategy_config(env: Mapping[str, str]) -> StrategyConfig:
    defaults = StrategyConfig()
    return StrategyConfig(
        fast_ema_period=_int(env, "STRATEGY_FAST_EMA", defaults.fast_ema_period),
        slow_ema_period=_int(env, "STRATEGY_SLOW_EMA", defaults.slow_ema_period),
        rsi_period=_int(env, "STRATEGY_RSI_PERIOD", defaults.rsi_period),
        atr_period=_int(env, "STRATEGY_ATR_PERIOD", defaults.atr_period),
        atr_multiplier=parse_number(_get(env, "STRATEGY_ATR_MULTIPLIER"),
                                    default=defaults.atr_multiplier, name="STRATEGY_ATR_MULTIPLIER"),
        max_stop_distance_pct=parse_number(_get(env, "STRATEGY_MAX_STOP_DISTANCE_PCT"),
                                           default=defaults.max_stop_distance_pct,
                                           name="STRATEGY_MAX_STOP_DISTANCE_PCT"),
        min_reward_risk=parse_number(_get(env, "STRATEGY_MIN_REWARD_RISK"),
                                     default=defaults.min_reward_risk, name="STRATEGY_MIN_REWARD_RISK"),
        bullish_rsi_min=parse_number(_get(env, "STRATEGY_BULLISH_RSI_MIN"),
                                     default=defaults.bullish_rsi_min, name="STRATEGY_BULLISH_RSI_MIN"),
        bullish_rsi_max=parse_number(_get(env, "STRATEGY_BULLISH_RSI_MAX"),
                                     default=defaults.bullish_rsi_max, name="STRATEGY_BULLISH_RSI_MAX"),
        bearish_rsi_min=parse_number(_get(env, "STRATEGY_BEARISH_RSI_MIN"),
                                     default=defaults.bearish_rsi_min, name="STRATEGY_BEARISH_RSI_MIN"),
        bearish_rsi_max=parse_number(_get(env, "STRATEGY_BEARISH_RSI_MAX"),
                                     default=defaults.bearish_rsi_max, name="STRATEGY_BEARISH_RSI_MAX"),
    )


def _risk_config(env: Mapping[str, str], *, max_candidate_price: float = 100.0) -> RiskConfig:
    defaults = RiskConfig()
    risk_amount = _get(env, "RISK_MAX_RISK_AMOUNT")
    # The risk layer's entry ceiling may only TIGHTEN the trading candidate
    # price limit: the effective value is the lower of the two, so risk can
    # never accept a price that the trading paths refuse before submission.
    entry_price = parse_number(_get(env, "RISK_MAX_ENTRY_PRICE"),
                               default=defaults.max_entry_price,
                               name="RISK_MAX_ENTRY_PRICE")
    return RiskConfig(
        risk_fraction=parse_number(_get(env, "RISK_FRACTION"), default=defaults.risk_fraction,
                                   name="RISK_FRACTION"),
        max_risk_per_trade=parse_number(_get(env, "RISK_MAX_PER_TRADE"),
                                        default=defaults.max_risk_per_trade, name="RISK_MAX_PER_TRADE"),
        max_risk_amount=(float(risk_amount) if risk_amount else None),
        max_position_notional=parse_number(_get(env, "RISK_MAX_POSITION_NOTIONAL"),
                                           default=defaults.max_position_notional,
                                           name="RISK_MAX_POSITION_NOTIONAL"),
        max_portfolio_exposure_fraction=parse_number(
            _get(env, "RISK_MAX_PORTFOLIO_EXPOSURE"), default=defaults.max_portfolio_exposure_fraction,
            name="RISK_MAX_PORTFOLIO_EXPOSURE"),
        max_simultaneous_positions=_int(env, "RISK_MAX_POSITIONS", defaults.max_simultaneous_positions),
        max_daily_loss_fraction=parse_number(_get(env, "RISK_MAX_DAILY_LOSS_FRACTION"),
                                             default=defaults.max_daily_loss_fraction,
                                             name="RISK_MAX_DAILY_LOSS_FRACTION"),
        min_reward_risk=parse_number(_get(env, "RISK_MIN_REWARD_RISK"), default=defaults.min_reward_risk,
                                     name="RISK_MIN_REWARD_RISK"),
        allow_duplicate_symbol=parse_bool(_get(env, "RISK_ALLOW_DUPLICATE_SYMBOL"),
                                          default=defaults.allow_duplicate_symbol),
        enabled=parse_bool(_get(env, "RISK_ENABLED"), default=defaults.enabled),
        max_quantity=_int(env, "RISK_MAX_QUANTITY", defaults.max_quantity),
        max_symbol_exposure=parse_number(_get(env, "RISK_MAX_SYMBOL_EXPOSURE"),
                                         default=defaults.max_symbol_exposure,
                                         name="RISK_MAX_SYMBOL_EXPOSURE"),
        max_entry_price=min(entry_price, float(max_candidate_price)),
        require_fresh_valuation=parse_bool(_get(env, "RISK_REQUIRE_FRESH_VALUATION"),
                                           default=defaults.require_fresh_valuation),
    )


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{key} must be an integer, received {raw!r}") from None


def _normalise_provider(raw: str | None, *, default: str = "none") -> str:
    """Collapse absent/development-only values to 'none' so the product never
    reports a test fixture as a connected integration."""
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value or value in {item.lower() for item in _DEV_ONLY_PROVIDERS}:
        return default
    return value


def _market_data_settings(env: Mapping[str, str]) -> MarketDataSettings:
    defaults = MarketDataSettings()
    provider = _normalise_provider(_get(env, "MARKET_DATA_PROVIDER"))
    provider_key = {"polygon": "POLYGON_API_KEY",
                    "alphavantage": "ALPHAVANTAGE_API_KEY",
                    "alpaca": "ALPACA_API_KEY"}.get(provider)
    if provider_key:
        api_key = _get(env, provider_key) or _get(env, "MARKET_DATA_API_KEY")
    else:
        api_key = (_get(env, "POLYGON_API_KEY")
                   or _get(env, "ALPHAVANTAGE_API_KEY")
                   or _get(env, "MARKET_DATA_API_KEY"))
    api_secret = (_get(env, "ALPACA_API_SECRET")
                  if provider == "alpaca" else None) or _get(env, "MARKET_DATA_API_SECRET")
    return MarketDataSettings(
        provider=provider,
        base_url=_get(env, "MARKET_DATA_BASE_URL"),
        api_key=api_key,
        api_secret=api_secret,
        plan=_get(env, "MARKET_DATA_PLAN"),
        account_id=_get(env, "MARKET_DATA_ACCOUNT_ID"),
        timeframe=_get(env, "MARKET_DATA_TIMEFRAME", "1min") or "1min",
        stale_after_seconds=parse_number(
            _get(env, "MARKET_DATA_STALE_AFTER_SECONDS"),
            default=defaults.stale_after_seconds,
            name="MARKET_DATA_STALE_AFTER_SECONDS"),
        timeout_seconds=parse_number(
            _get(env, "MARKET_DATA_TIMEOUT_SECONDS"),
            default=defaults.timeout_seconds,
            name="MARKET_DATA_TIMEOUT_SECONDS"),
        stream=parse_bool(_get(env, "MARKET_DATA_STREAM"), default=defaults.stream),
        stream_feed=(_get(env, "MARKET_DATA_STREAM_FEED") or "").strip().lower(),
        stream_url=_get(env, "MARKET_DATA_STREAM_URL"),
    )


def _broker_settings(env: Mapping[str, str]) -> BrokerSettings:
    raw = _get(env, "TRADING_PLATFORM_PROVIDER") or _get(env, "TRADING_PLATFORM_ADAPTER")
    provider = _normalise_provider(raw)
    environment = (_get(env, "TRADING_PLATFORM_ENVIRONMENT", "SANDBOX") or "SANDBOX").upper()
    if environment == "PAPER":
        environment = "SANDBOX"
    return BrokerSettings(
        provider=provider,
        environment=environment,
        base_url=_get(env, "TRADING_PLATFORM_BASE_URL"),
        api_key=_get(env, "ALPACA_API_KEY") or _get(env, "TRADING_PLATFORM_API_KEY"),
        api_secret=_get(env, "ALPACA_API_SECRET") or _get(env, "TRADING_PLATFORM_API_SECRET"),
        account_id=_get(env, "ALPACA_ACCOUNT_ID") or _get(env, "TRADING_PLATFORM_ACCOUNT_ID"),
    )


def _ai_settings(env: Mapping[str, str]) -> AISettings:
    raw = (_get(env, "AI_PROVIDER") or "local").strip().upper()
    provider = {"MOCK": "local", "LOCAL/MOCK": "local", "LOCAL": "local",
                "NONE": "none", "DISABLED": "none"}.get(raw, raw.lower())
    return AISettings(
        enabled=parse_bool(_get(env, "AI_ENABLED"), default=True),
        provider=provider,
        model=_get(env, "AI_MODEL") or ("local-rule-based" if provider == "local" else "unset"),
        base_url=_get(env, "AI_BASE_URL"),
        api_key=_get(env, "AI_API_KEY"),
    )


def _notification_settings(env: Mapping[str, str]) -> NotificationSettings:
    enabled = parse_bool(_get(env, "NOTIFICATION_ENABLED"), default=True)
    primary = (_get(env, "NOTIFICATION_PROVIDER") or "local").strip().lower()
    webhook_url = _get(env, "NOTIFICATION_WEBHOOK_URL") or _get(env, "NOTIFICATION_DESTINATION")
    channels = (
        NotificationChannelSettings(
            provider="local", enabled=True,
            destination=_get(env, "NOTIFICATION_DESTINATION"),
        ),
        NotificationChannelSettings(
            provider="webhook",
            enabled=enabled and (primary == "webhook" or bool(webhook_url)),
            destination=webhook_url,
            token=_get(env, "NOTIFICATION_TOKEN"),
        ),
        NotificationChannelSettings(
            provider="discord",
            enabled=enabled and (primary == "discord" or bool(_get(env, "DISCORD_WEBHOOK_URL"))),
            destination=_get(env, "DISCORD_WEBHOOK_URL"),
        ),
        NotificationChannelSettings(
            provider="telegram",
            enabled=enabled and (primary == "telegram" or bool(_get(env, "TELEGRAM_BOT_TOKEN"))),
            destination=_get(env, "TELEGRAM_CHAT_ID"),
            token=_get(env, "TELEGRAM_BOT_TOKEN"),
        ),
    )
    return NotificationSettings(provider=primary, enabled=enabled, channels=channels)
