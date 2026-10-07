"""Provider-independent, read-only market data models and interfaces."""

from .bridge import (
    CONNECTED,
    ERROR,
    MarketDataBridge,
    NOT_CONFIGURED,
    PRICED,
    QUOTE_UNPRICED,
    STALE,
    UNAVAILABLE,
    BridgeReport,
    SymbolValuation,
)
from .models import Bar, Quote
from .provider import MarketDataProvider, MarketDataProviderError
from .registry import build_market_data_provider, is_implemented
from .service import MarketDataService
from .session import USMarketSession, market_session
from .unconfigured import UnconfiguredMarketDataProvider
from .validation import DataValidationError, normalize_bar, normalize_quote

__all__ = [
    "Bar",
    "BridgeReport",
    "CONNECTED",
    "DataValidationError",
    "ERROR",
    "MarketDataBridge",
    "MarketDataProvider",
    "MarketDataProviderError",
    "MarketDataService",
    "NOT_CONFIGURED",
    "PRICED",
    "QUOTE_UNPRICED",
    "Quote",
    "STALE",
    "SymbolValuation",
    "UNAVAILABLE",
    "USMarketSession",
    "UnconfiguredMarketDataProvider",
    "build_market_data_provider",
    "is_implemented",
    "market_session",
    "normalize_bar",
    "normalize_quote",
]
