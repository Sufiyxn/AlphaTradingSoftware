"""Builds the product market-data provider from configuration.

A provider is usable only when the integration catalog declares the adapter
implemented *and* this module has a builder registered for it. Anything else -
including a vendor whose credentials are present but whose adapter is not
installed - resolves to :class:`UnconfiguredMarketDataProvider`, so the product
reports the gap instead of silently falling back to fabricated data.
"""
from collections.abc import Callable

from backend.config.catalog import descriptor

from .alphavantage import AlphaVantageMarketDataProvider
from .polygon import PolygonMarketDataProvider
from .provider import MarketDataProvider
from .unconfigured import UnconfiguredMarketDataProvider

MarketDataBuilder = Callable[..., MarketDataProvider]


def _vendor_kwargs(settings, transport, clock) -> dict:
    return {
        "api_key": getattr(settings, "api_key", None) or "",
        "base_url": getattr(settings, "base_url", None) or None,
        "transport": transport,
        "clock": clock,
        "timeout_seconds": getattr(settings, "timeout_seconds", 10.0),
        "stale_after_seconds": getattr(settings, "stale_after_seconds", 300.0),
        "timeframe": getattr(settings, "timeframe", "1min"),
    }


def _build_polygon(settings, *, transport=None, clock=None) -> MarketDataProvider:
    return PolygonMarketDataProvider(**_vendor_kwargs(settings, transport, clock))


def _build_alphavantage(settings, *, transport=None, clock=None) -> MarketDataProvider:
    return AlphaVantageMarketDataProvider(**_vendor_kwargs(settings, transport, clock))


IMPLEMENTED: dict[str, MarketDataBuilder] = {
    "polygon": _build_polygon,
    "alphavantage": _build_alphavantage,
}


def is_implemented(provider: str) -> bool:
    """True only for adapters this build can actually instantiate."""
    provider_id = (provider or "none").strip().lower()
    if provider_id == "none":
        return False
    found = descriptor("market_data", provider_id)
    return bool(found and found.implemented and provider_id in IMPLEMENTED)


def build_market_data_provider(settings, *, transport=None, clock=None) -> MarketDataProvider:
    provider = (getattr(settings, "provider", None) or "none").strip().lower()
    if provider == "none":
        return UnconfiguredMarketDataProvider()
    builder = IMPLEMENTED.get(provider)
    found = descriptor("market_data", provider)
    if builder is None or found is None or not found.implemented:
        return UnconfiguredMarketDataProvider(
            provider, f"the {provider} market data adapter is not installed")
    if not getattr(settings, "configured", True):
        return UnconfiguredMarketDataProvider(
            provider, f"the {provider} market data adapter is installed but has no credentials")
    return builder(settings, transport=transport, clock=clock)
