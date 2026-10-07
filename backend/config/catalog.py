"""Static catalogs of the integrations this build knows how to talk to.

Catalogs drive configuration validation, the Connection Center, capability
reports, and the dashboard. ``implemented=False`` entries are advertised so a
user can see they exist, but are always reported as NOT_IMPLEMENTED and can
never be selected as a working integration.

Nothing here performs I/O and nothing here contains credentials.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class IntegrationDescriptor:
    id: str
    label: str
    kind: str
    implemented: bool
    environments: tuple[str, ...] = ()
    required_credentials: tuple[str, ...] = ()
    optional_fields: tuple[str, ...] = ()
    markets: tuple[str, ...] = ()
    timeframes: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    secret_fields: tuple[str, ...] = ()

    @property
    def requires_credentials(self) -> bool:
        return bool(self.required_credentials)


MARKET_DATA_CATALOG: dict[str, IntegrationDescriptor] = {
    descriptor.id: descriptor
    for descriptor in (
        IntegrationDescriptor(
            id="polygon", label="Polygon", kind="market_data", implemented=True,
            required_credentials=("POLYGON_API_KEY",),
            optional_fields=("MARKET_DATA_BASE_URL", "MARKET_DATA_PLAN"),
            markets=("US_EQUITIES", "US_INDICES"),
            timeframes=("1min", "5min", "15min", "1h", "1day"),
            capabilities=("historical_ohlcv", "latest_quotes", "symbol_lookup", "read_only"),
            secret_fields=("POLYGON_API_KEY",),
        ),
        IntegrationDescriptor(
            id="alphavantage", label="Alpha Vantage", kind="market_data", implemented=True,
            required_credentials=("ALPHAVANTAGE_API_KEY",),
            optional_fields=("MARKET_DATA_BASE_URL",),
            markets=("US_EQUITIES", "FX", "CRYPTO"),
            timeframes=("1min", "5min", "15min", "30min", "60min", "daily"),
            capabilities=("historical_ohlcv", "latest_quotes", "symbol_lookup", "read_only"),
            secret_fields=("ALPHAVANTAGE_API_KEY",),
        ),
    )
}

BROKER_CATALOG: dict[str, IntegrationDescriptor] = {
    descriptor.id: descriptor
    for descriptor in (
        IntegrationDescriptor(
            id="alpaca", label="Alpaca", kind="broker", implemented=False,
            environments=("SANDBOX", "LIVE"),
            required_credentials=("ALPACA_API_KEY", "ALPACA_API_SECRET"),
            optional_fields=("ALPACA_ACCOUNT_ID", "TRADING_PLATFORM_BASE_URL"),
            markets=("US_EQUITIES", "US_INDICES"),
            timeframes=(),
            capabilities=("orders", "replace_order", "cancel_order", "positions",
                          "account", "market_data", "short_selling", "fractional_shares",
                          "extended_hours"),
            secret_fields=("ALPACA_API_KEY", "ALPACA_API_SECRET"),
        ),
    )
}

NOTIFICATION_CATALOG: dict[str, IntegrationDescriptor] = {
    descriptor.id: descriptor
    for descriptor in (
        IntegrationDescriptor(
            id="local", label="Local journal sink", kind="notification", implemented=True,
            required_credentials=(),
            capabilities=("in_process",),
        ),
        IntegrationDescriptor(
            id="webhook", label="Generic webhook", kind="notification", implemented=False,
            required_credentials=("NOTIFICATION_WEBHOOK_URL",),
            optional_fields=("NOTIFICATION_TOKEN",),
            capabilities=("http_post_json",),
            secret_fields=("NOTIFICATION_TOKEN",),
        ),
        IntegrationDescriptor(
            id="discord", label="Discord", kind="notification", implemented=False,
            required_credentials=("DISCORD_WEBHOOK_URL",),
            capabilities=("http_post_json",),
        ),
        IntegrationDescriptor(
            id="telegram", label="Telegram", kind="notification", implemented=False,
            required_credentials=("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
            capabilities=("http_post_json",),
            secret_fields=("TELEGRAM_BOT_TOKEN",),
        ),
    )
}

AI_CATALOG: dict[str, IntegrationDescriptor] = {
    descriptor.id: descriptor
    for descriptor in (
        IntegrationDescriptor(
            id="none", label="Disabled", kind="ai", implemented=True,
            required_credentials=(),
            capabilities=(),
        ),
        IntegrationDescriptor(
            id="local", label="Local rule-based analysis", kind="ai", implemented=True,
            required_credentials=(),
            capabilities=("setup_review", "scoring", "advisory_only"),
        ),
        IntegrationDescriptor(
            id="openai_compatible", label="OpenAI-compatible endpoint", kind="ai",
            implemented=False,
            required_credentials=("AI_API_KEY",),
            optional_fields=("AI_BASE_URL", "AI_MODEL"),
            capabilities=("setup_review", "scoring", "advisory_only"),
            secret_fields=("AI_API_KEY",),
        ),
    )
}

CATALOGS: dict[str, dict[str, IntegrationDescriptor]] = {
    "market_data": MARKET_DATA_CATALOG,
    "broker": BROKER_CATALOG,
    "notification": NOTIFICATION_CATALOG,
    "ai": AI_CATALOG,
}


def descriptor(kind: str, integration_id: str) -> IntegrationDescriptor | None:
    return CATALOGS.get(kind, {}).get(integration_id)


def is_known(kind: str, integration_id: str) -> bool:
    return descriptor(kind, integration_id) is not None


def is_selectable(kind: str, integration_id: str) -> bool:
    """Selectable means the product can actually use it (never 'none' placeholders)."""
    found = descriptor(kind, integration_id)
    return found is not None and found.implemented
