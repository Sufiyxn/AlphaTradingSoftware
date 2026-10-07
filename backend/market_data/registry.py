"""Builds the product market-data provider from configuration.

A provider is usable only when the integration catalog declares the adapter
implemented *and* this module has a builder registered for it. Anything else -
including a vendor whose credentials are present but whose adapter is not
installed - resolves to :class:`UnconfiguredMarketDataProvider`, so the product
reports the gap instead of silently falling back to fabricated data.
"""
from collections.abc import Callable

from backend.config.catalog import descriptor

from .provider import MarketDataProvider
from .unconfigured import UnconfiguredMarketDataProvider

MarketDataBuilder = Callable[..., MarketDataProvider]

IMPLEMENTED: dict[str, MarketDataBuilder] = {}


def is_implemented(provider: str) -> bool:
    """True only for adapters this build can actually instantiate."""
    provider_id = (provider or "none").strip().lower()
    if provider_id == "none":
        return False
    found = descriptor("market_data", provider_id)
    return bool(found and found.implemented and provider_id in IMPLEMENTED)


def build_market_data_provider(settings) -> MarketDataProvider:
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
    return builder(settings)
