"""External-integration configuration sections.

Every section separates "is it configured?" from "what is the secret?".
``configured`` is computed from credential *presence*; ``describe()`` reports
only presence booleans and never a credential value.
"""
from dataclasses import dataclass

from .catalog import (
    AI_CATALOG,
    BROKER_CATALOG,
    MARKET_DATA_CATALOG,
    NOTIFICATION_CATALOG,
    descriptor,
)


def _missing(descriptor_id: str, catalog: dict, field_names: tuple[str, ...],
             values: dict[str, str | None]) -> tuple[str, ...]:
    return tuple(name for name in field_names if not values.get(name))


@dataclass(frozen=True, slots=True)
class MarketDataSettings:
    provider: str = "none"
    base_url: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    plan: str | None = None
    account_id: str | None = None
    timeframe: str = "1min"
    stale_after_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")

    @property
    def known(self) -> bool:
        return descriptor("market_data", self.provider) is not None

    @property
    def entry(self):
        return MARKET_DATA_CATALOG.get(self.provider)

    @property
    def configured(self) -> bool:
        found = self.entry
        if found is None:
            return False
        values = {"POLYGON_API_KEY": self.api_key, "ALPHAVANTAGE_API_KEY": self.api_key}
        return not _missing(self.provider, MARKET_DATA_CATALOG, found.required_credentials, values)

    @property
    def missing_credentials(self) -> tuple[str, ...]:
        found = self.entry
        if found is None:
            return ()
        values = {"POLYGON_API_KEY": self.api_key, "ALPHAVANTAGE_API_KEY": self.api_key}
        return _missing(self.provider, MARKET_DATA_CATALOG, found.required_credentials, values)

    @property
    def implemented(self) -> bool:
        return bool(self.entry and self.entry.implemented)

    @property
    def available(self) -> bool:
        return self.known and self.implemented and self.configured

    @property
    def state(self) -> str:
        return "CONFIGURED" if self.available else "NOT_CONFIGURED"

    @property
    def unavailable_reason(self) -> str | None:
        if self.provider == "none":
            return "no market data provider is connected"
        if not self.known:
            return f"{self.provider} is not a known market data provider"
        if not self.implemented:
            return f"the {self.provider} market data adapter is not installed"
        if not self.configured:
            return f"the {self.provider} market data adapter has no credentials"
        return None

    def describe(self) -> dict:
        found = self.entry
        return {
            "provider": self.provider,
            "label": found.label if found else self.provider,
            "known": self.known,
            "implemented": bool(found and found.implemented),
            "configured": self.configured,
            "available": self.available,
            "state": self.state,
            "reason": self.unavailable_reason,
            "missing_credentials": list(self.missing_credentials),
            "base_url_configured": bool(self.base_url),
            "plan_configured": bool(self.plan),
            "account_id_configured": bool(self.account_id),
            "timeframe": self.timeframe,
            "stale_after_seconds": self.stale_after_seconds,
            "markets": list(found.markets) if found else [],
            "timeframes": list(found.timeframes) if found else [],
            "capabilities": list(found.capabilities) if found else [],
            "credential_fields": [
                {"name": name, "configured": bool(value)}
                for name, value in (
                    ("API key", self.api_key),
                    ("API secret", self.api_secret),
                )
            ],
        }


@dataclass(frozen=True, slots=True)
class BrokerSettings:
    provider: str = "none"
    environment: str = "SANDBOX"
    base_url: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    account_id: str | None = None

    @property
    def known(self) -> bool:
        return descriptor("broker", self.provider) is not None

    @property
    def entry(self):
        return BROKER_CATALOG.get(self.provider)

    @property
    def environment_valid(self) -> bool:
        found = self.entry
        if found is None:
            return False
        return not found.environments or self.environment in found.environments

    @property
    def configured(self) -> bool:
        found = self.entry
        if found is None or not self.environment_valid:
            return False
        values = {"ALPACA_API_KEY": self.api_key, "ALPACA_API_SECRET": self.api_secret}
        return not _missing(self.provider, BROKER_CATALOG, found.required_credentials, values)

    @property
    def missing_credentials(self) -> tuple[str, ...]:
        found = self.entry
        if found is None:
            return ()
        values = {"ALPACA_API_KEY": self.api_key, "ALPACA_API_SECRET": self.api_secret}
        return _missing(self.provider, BROKER_CATALOG, found.required_credentials, values)

    @property
    def implemented(self) -> bool:
        return bool(self.entry and self.entry.implemented)

    @property
    def available(self) -> bool:
        return self.known and self.implemented and self.configured

    @property
    def state(self) -> str:
        return "CONFIGURED" if self.available else "NOT_CONFIGURED"

    @property
    def unavailable_reason(self) -> str | None:
        if self.provider == "none":
            return "no broker is connected"
        if not self.known:
            return f"{self.provider} is not a known broker"
        if not self.environment_valid:
            return f"{self.environment} is not a supported environment for {self.provider}"
        if not self.implemented:
            return f"the {self.provider} broker adapter is not installed"
        if not self.configured:
            return f"the {self.provider} broker adapter has no credentials"
        return None

    def describe(self) -> dict:
        found = self.entry
        return {
            "provider": self.provider,
            "label": found.label if found else self.provider,
            "known": self.known,
            "implemented": bool(found and found.implemented),
            "configured": self.configured,
            "available": self.available,
            "state": self.state,
            "reason": self.unavailable_reason,
            "environment": self.environment,
            "environment_valid": self.environment_valid,
            "supported_environments": list(found.environments) if found else [],
            "missing_credentials": list(self.missing_credentials),
            "base_url_configured": bool(self.base_url),
            "account_id_configured": bool(self.account_id),
            "capabilities": list(found.capabilities) if found else [],
            "credential_fields": [
                {"name": name, "configured": bool(value)}
                for name, value in (("API key", self.api_key), ("API secret", self.api_secret))
            ],
            "live_execution_available": False,
        }


@dataclass(frozen=True, slots=True)
class TradingViewSettings:
    enabled: bool = False
    webhook_secret: str | None = None
    allowed_strategies: tuple[str, ...] = ()
    max_body_bytes: int = 8192
    max_signal_age_seconds: float = 300.0
    stale_after_seconds: float = 86_400.0

    def __post_init__(self) -> None:
        if self.max_body_bytes <= 0 or self.max_body_bytes > 65536:
            raise ValueError("max_body_bytes must be between 1 and 65536")
        if self.max_signal_age_seconds <= 0:
            raise ValueError("max_signal_age_seconds must be positive")
        if self.stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")

    @property
    def configured(self) -> bool:
        return bool(self.webhook_secret)

    @property
    def ready(self) -> bool:
        """Endpoint can accept and authenticate an alert right now."""
        return self.enabled and self.configured

    @property
    def available(self) -> bool:
        return self.ready

    def describe(self) -> dict:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "ready": self.ready,
            "available": self.available,
            "secret_configured": bool(self.webhook_secret),
            "authentication": "HMAC_SHARED_SECRET" if self.webhook_secret else "UNAVAILABLE",
            "allowed_strategies": list(self.allowed_strategies),
            "max_body_bytes": self.max_body_bytes,
            "max_signal_age_seconds": self.max_signal_age_seconds,
            "stale_after_seconds": self.stale_after_seconds,
            "endpoint": "/api/webhooks/tradingview",
            "role": "SIGNAL_SOURCE_ONLY",
        }


@dataclass(frozen=True, slots=True)
class AISettings:
    enabled: bool = True
    provider: str = "local"
    model: str = "local-rule-based"
    base_url: str | None = None
    api_key: str | None = None

    @property
    def entry(self):
        return AI_CATALOG.get(self.provider)

    @property
    def known(self) -> bool:
        return self.entry is not None

    @property
    def configured(self) -> bool:
        found = self.entry
        if found is None:
            return False
        if not found.required_credentials:
            return True
        values = {"AI_API_KEY": self.api_key}
        return not _missing(self.provider, AI_CATALOG, found.required_credentials, values)

    @property
    def implemented(self) -> bool:
        return bool(self.entry and self.entry.implemented)

    @property
    def reason(self) -> str | None:
        return self._unavailable_reason()

    @property
    def state(self) -> str:
        return "CONFIGURED" if self.active else "NOT_CONFIGURED"

    @property
    def active(self) -> bool:
        return self.enabled and self.provider != "none" and self.configured and self.implemented

    @property
    def missing_credentials(self) -> tuple[str, ...]:
        found = self.entry
        if found is None:
            return ()
        values = {"AI_API_KEY": self.api_key}
        return _missing(self.provider, AI_CATALOG, found.required_credentials, values)

    def describe(self) -> dict:
        found = self.entry
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "label": found.label if found else self.provider,
            "model": self.model,
            "known": self.known,
            "implemented": self.implemented,
            "configured": self.configured,
            "available": self.configured and self.implemented,
            "active": self.active,
            "state": "CONFIGURED" if self.active else "NOT_CONFIGURED",
            "reason": None if self.active else self._unavailable_reason(),
            "missing_credentials": list(self.missing_credentials),
            "endpoint_configured": bool(self.base_url),
            "role": "ADVISORY_ONLY",
            "authority": "NONE",
        }

    def _unavailable_reason(self) -> str:
        if not self.enabled:
            return "AI analysis is disabled"
        if not self.configured:
            return f"the {self.provider} AI adapter is not configured"
        if not self.implemented:
            return f"the {self.provider} AI adapter is not installed"
        return f"the {self.provider} AI adapter is not active"


@dataclass(frozen=True, slots=True)
class NotificationChannelSettings:
    provider: str
    enabled: bool = False
    destination: str | None = None
    token: str | None = None
    chat_id: str | None = None

    @property
    def entry(self):
        return NOTIFICATION_CATALOG.get(self.provider)

    @property
    def known(self) -> bool:
        return self.entry is not None

    @property
    def configured(self) -> bool:
        found = self.entry
        if found is None:
            return False
        values = {
            "NOTIFICATION_WEBHOOK_URL": self.destination,
            "DISCORD_WEBHOOK_URL": self.destination,
            "TELEGRAM_BOT_TOKEN": self.token,
            "TELEGRAM_CHAT_ID": self.chat_id,
        }
        return not _missing(self.provider, NOTIFICATION_CATALOG, found.required_credentials, values)

    @property
    def implemented(self) -> bool:
        return bool(self.entry and self.entry.implemented)

    @property
    def active(self) -> bool:
        if self.provider == "local":
            return True
        return self.enabled and self.configured and self.implemented

    def describe(self) -> dict:
        found = self.entry
        return {
            "provider": self.provider,
            "label": found.label if found else self.provider,
            "enabled": self.enabled,
            "known": self.known,
            "implemented": self.implemented,
            "configured": self.configured,
            "available": self.configured and self.implemented,
            "active": self.active,
            "state": "CONFIGURED" if self.active else "NOT_CONFIGURED",
            "reason": None if self.active else self._unavailable_reason(),
            "destination_configured": bool(self.destination),
            "token_configured": bool(self.token),
            "capabilities": list(found.capabilities) if found else [],
        }

    def _unavailable_reason(self) -> str:
        if not self.configured:
            return f"the {self.provider} notification channel is not configured"
        if not self.implemented:
            return f"the {self.provider} notification adapter is not installed"
        return f"the {self.provider} notification channel is not active"


@dataclass(frozen=True, slots=True)
class NotificationSettings:
    provider: str = "local"
    enabled: bool = True
    channels: tuple[NotificationChannelSettings, ...] = ()

    @property
    def entry(self):
        return NOTIFICATION_CATALOG.get(self.provider)

    @property
    def known(self) -> bool:
        return self.entry is not None

    @property
    def implemented(self) -> bool:
        return bool(self.entry and self.entry.implemented)

    @property
    def active_channels(self) -> tuple[NotificationChannelSettings, ...]:
        return tuple(channel for channel in self.channels if channel.active)

    @property
    def configured(self) -> bool:
        return any(channel.configured for channel in self.channels)

    @property
    def label(self) -> str:
        return self.entry.label if self.entry else self.provider

    @property
    def reason(self) -> str | None:
        return self._unavailable_reason()

    @property
    def state(self) -> str:
        return "CONFIGURED" if self.active_channels else "NOT_CONFIGURED"

    def describe(self) -> dict:
        return {
            "provider": self.provider,
            "label": self.label,
            "enabled": self.enabled,
            "known": self.known,
            "implemented": self.implemented,
            "configured": self.configured,
            "active": bool(self.active_channels),
            "state": "CONFIGURED" if self.active_channels else "NOT_CONFIGURED",
            "reason": None if self.active_channels else "no notification channel is active",
            "active_channels": [channel.provider for channel in self.active_channels],
            "channels": [channel.describe() for channel in self.channels],
            "events": [
                "TRADE_OPENED", "TRADE_CLOSED", "STOP_LOSS", "TAKE_PROFIT",
                "RISK_REJECTED", "DAILY_REPORT", "SYSTEM_ERROR",
                "EMERGENCY_STOP", "BROKER_CONNECTION_FAILURE",
                "TV_SIGNAL_RECEIVED", "TV_SIGNAL_REJECTED", "MODE_CHANGED",
            ],
        }
