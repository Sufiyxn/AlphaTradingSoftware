from datetime import datetime, timedelta, timezone
from typing import Protocol

from backend.market_data import DataValidationError, MarketDataProviderError, MarketDataService
from backend.market_data.series import NO_DATA, OK, CandleSeriesService
from backend.market_data.models import Bar

from .analysis import AnalysisContext, PerSymbolAnalyzer
from .models import Opportunity, SymbolResult
from .ranking import filter_and_rank
from .universe import SymbolUniverse

#: Frames that only produce bars while a session is actually trading.
INTRADAY_FRAMES = ("1min", "5min", "15min", "30min", "1h", "60min")

#: Coarser frames offered in order when the intraday window is empty (weekend,
#: holiday, or before the first bar of the session). Daily bars are genuine
#: historical data and are labelled as such by the caller - never presented as
#: a live intraday feed.
DAILY_FALLBACK_FRAMES = ("1day", "daily")


def daily_frame_for(series: CandleSeriesService) -> str | None:
    """The first daily frame the connected provider advertises, if any.

    Shared by the scanner, the watchlist, and the orchestrator so the
    intraday-to-daily fallback picks the same frame everywhere.
    """
    supported = series.supported_timeframes()
    return next((frame for frame in DAILY_FALLBACK_FRAMES if frame in supported), None)


class MarketScanner(Protocol):
    def scan(self, universe: SymbolUniverse) -> tuple[Opportunity, ...]:
        ...


class MarketScannerService:
    """Collect validated data, isolate per-symbol failures, analyze, filter, and rank.

    Bar loading prefers the configured intraday timeframe and falls back to the
    provider's daily frame when the intraday window holds no bars at all - the
    weekend case that otherwise leaves a scanner blind until Monday. The frame
    actually used is recorded per symbol so freshness and the UI can label it
    honestly.
    """

    def __init__(
        self,
        market_data: MarketDataService,
        analyzer: PerSymbolAnalyzer,
        *,
        lookback: timedelta = timedelta(minutes=3),
        minimum_reward_risk: float = 2.0,
        clock=lambda: datetime.now(timezone.utc),
        series: CandleSeriesService | None = None,
        timeframe: str = "1min",
        daily_lookback_days: float = 90.0,
    ):
        if lookback <= timedelta(0):
            raise ValueError("lookback must be positive")
        if minimum_reward_risk < 2.0:
            raise ValueError("minimum_reward_risk cannot be below 2.0")
        if daily_lookback_days <= 0:
            raise ValueError("daily_lookback_days must be positive")
        self._market_data = market_data
        self._analyzer = analyzer
        self._lookback = lookback
        self._minimum_reward_risk = minimum_reward_risk
        self._clock = clock
        #: When a series service is wired, bar loading becomes timeframe-aware
        #: and validated; without it the legacy direct path is used, which the
        #: unit tests rely on.
        self._series = series
        self._timeframe = timeframe
        self._daily_lookback_days = daily_lookback_days

    @property
    def timeframe(self) -> str:
        return self._timeframe

    def scan(self, universe: SymbolUniverse) -> tuple[Opportunity, ...]:
        return self.scan_detailed(universe)[0]

    def scan_detailed(self, universe: SymbolUniverse) -> tuple[tuple[Opportunity, ...], tuple[SymbolResult, ...]]:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("scanner clock must return a timezone-aware datetime")
        now = now.astimezone(timezone.utc)
        opportunities: list[Opportunity] = []
        results: list[SymbolResult] = []
        # Batch per bounded chunk through the existing market-data service; errors stay isolated by symbol.
        for symbol in universe.symbols:
            try:
                bars, frame, load_note = self._load_bars(symbol, now)
                if not bars:
                    results.append(SymbolResult(
                        symbol, None,
                        load_note or "No historical data available",
                        timeframe=frame))
                    continue
                opportunity = self._analyzer.analyze(
                    AnalysisContext(symbol, bars, bars[-1].timestamp, timeframe=frame))
                if opportunity.symbol != symbol:
                    raise DataValidationError("analyzer returned an opportunity for a different symbol")
                opportunities.append(opportunity)
                results.append(SymbolResult(symbol, opportunity, timeframe=frame,
                                            bar_count=len(bars),
                                            latest_bar_at=bars[-1].timestamp))
            except (DataValidationError, MarketDataProviderError, ValueError) as error:
                results.append(SymbolResult(symbol, None, str(error)))
            except Exception as error:
                results.append(SymbolResult(symbol, None, f"analysis failed: {error}"))
        ranked = filter_and_rank(opportunities, self._minimum_reward_risk)
        return ranked, tuple(results)

    def _load_bars(self, symbol: str, now: datetime) -> tuple[tuple[Bar, ...], str | None, str | None]:
        """Bars for one symbol plus the frame used and an optional note.

        Returns ``(bars, timeframe, note)``. ``note`` explains an empty result;
        an empty tuple with no note means the provider genuinely had nothing.
        """
        if self._series is None:
            start = now - self._lookback
            return tuple(self._market_data.get_historical(symbol, start, now)), None, None

        frame = self._timeframe
        series = self._series.get(symbol, timeframe=frame)
        if series.state is NO_DATA and frame in INTRADAY_FRAMES:
            daily = self._daily_frame()
            if daily is not None:
                retry = self._series.get(symbol, timeframe=daily,
                                         days=self._daily_lookback_days)
                if retry.state is OK and retry.bars:
                    note = (f"no {frame} bars in the recent window (session closed); "
                            f"analyzed the latest completed {daily} bars instead")
                    return retry.bars, daily, note
                if retry.state is not OK:
                    note = retry.reason or f"daily market data is {retry.state}"
                    return (), daily, note
        if series.state is not OK:
            note = series.reason or f"market data is {series.state}"
            return (), frame, note
        return series.bars, frame, None

    def _daily_frame(self) -> str | None:
        """The first daily frame the connected provider advertises, if any."""
        return daily_frame_for(self._series)
