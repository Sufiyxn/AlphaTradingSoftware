"""Applies real quotes to paper positions and evaluates automatic exits.

This is the only production path that moves a paper position's market price.
Every price originates from :class:`MarketDataBridge`, which reads a connected
market-data provider. Nothing here derives a price from an entry, a signal, a
candle series, or an arithmetic guess, and a symbol the provider does not cover
stays explicitly UNPRICED with the reason recorded.
"""
import logging

from backend.market_data.bridge import (
    CONNECTED,
    PRICED,
    QUOTE_UNPRICED,
    STALE,
    BridgeReport,
    MarketDataBridge,
)
from backend.portfolio import Portfolio
from backend.portfolio.models import PriceState

from .engine import PaperTradingEngine

_PRICE_STATES = {
    PRICED: PriceState.FRESH,
    STALE: PriceState.STALE,
    QUOTE_UNPRICED: PriceState.UNPRICED,
}

_UNPRICED_DEFAULT = "no usable real quote is available for this symbol"


class PositionValuationService:
    """Marks paper positions from real quotes and gates automatic exits."""

    def __init__(self, bridge: MarketDataBridge, portfolio: Portfolio,
                 paper: PaperTradingEngine, *, journal=None):
        self.bridge = bridge
        self.portfolio = portfolio
        self.paper = paper
        self.journal = journal
        self._last_report: BridgeReport | None = None
        self._last_availability: str | None = None

    @property
    def last_report(self) -> BridgeReport | None:
        return self._last_report

    def availability(self) -> dict:
        return self.bridge.availability()

    def refresh(self, symbols) -> BridgeReport:
        """Fetch real quotes, mark open positions, then evaluate automatic exits."""
        open_symbols = {position.symbol for position in self.portfolio.positions}
        requested = tuple(dict.fromkeys(
            [*(symbol.strip().upper() for symbol in symbols if isinstance(symbol, str) and symbol.strip()),
             *sorted(open_symbols)]))
        report = self.bridge.refresh(requested)
        self._last_report = report

        for valuation in report.valuations:
            if valuation.symbol not in open_symbols:
                continue
            self._mark(valuation)

        # Automatic stop-loss/take-profit only ever sees a FRESH real mark.
        for valuation in report.valuations:
            if valuation.state != PRICED or valuation.symbol not in open_symbols:
                continue
            if valuation.price is None or valuation.as_of is None:
                continue
            try:
                self.paper.on_quote(valuation.symbol, valuation.as_of, valuation.price)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Automatic exit evaluation failed for %s", valuation.symbol)

        if (self.journal is not None
                and report.availability != self._last_availability
                and report.availability != CONNECTED):
            self._record_degraded(report)
        self._last_availability = report.availability
        return report

    def _mark(self, valuation) -> None:
        state = _PRICE_STATES.get(valuation.state, PriceState.UNPRICED)
        if valuation.price is None or valuation.as_of is None:
            self.portfolio.mark_unpriced(
                valuation.symbol, reason=valuation.reason or _UNPRICED_DEFAULT)
            return
        self.portfolio.update_price(valuation.symbol, valuation.price,
                                    timestamp=valuation.as_of, state=state,
                                    reason=valuation.reason)

    def _record_degraded(self, report: BridgeReport) -> None:
        try:
            self.journal.record_event(
                report.refreshed_at, "MARKET_DATA_UNAVAILABLE",
                report.reason or "market data is unavailable",
                context={"availability": report.availability,
                         "stale_after_seconds": report.stale_after_seconds,
                         "affected_symbols": [item.symbol for item in report.valuations]})
        except Exception:
            logging.getLogger(__name__).exception("Unable to persist market-data availability event")
