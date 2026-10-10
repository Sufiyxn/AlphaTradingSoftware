"""Universe watchlist rows derived from validated candles.

Every number here comes from the provider-validated candle series - last
close, change against the previous close, volume, and indicators computed
only when enough chronological bars exist. A symbol with no usable data is a
row with an honest reason, never a missing row and never an invented price.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from backend.market_data.series import (
    NO_DATA,
    NOT_CONFIGURED,
    OK,
    CandleSeriesService,
)
from backend.strategies.indicators import atr, ema, rsi

PRICED = "PRICED"
UNPRICED = "UNPRICED"

INDICATORS_OK = "OK"
INDICATORS_INSUFFICIENT = "INSUFFICIENT_DATA"
INDICATORS_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class WatchlistRow:
    symbol: str
    state: str
    reason: str | None
    last_price: float | None
    last_price_at: datetime | None
    previous_close: float | None
    change_percent: float | None
    volume: float | None
    ema: float | None
    rsi: float | None
    atr: float | None
    indicator_state: str
    indicator_reason: str | None
    bar_count: int
    freshness: str | None
    age_seconds: float | None

    def payload(self) -> dict:
        return {
            "symbol": self.symbol,
            "state": self.state,
            "reason": self.reason,
            "last_price": self.last_price,
            "last_price_at": self.last_price_at.isoformat() if self.last_price_at else None,
            "previous_close": self.previous_close,
            "change_percent": self.change_percent,
            "volume": self.volume,
            "ema": self.ema,
            "rsi": self.rsi,
            "atr": self.atr,
            "indicator_state": self.indicator_state,
            "indicator_reason": self.indicator_reason,
            "bar_count": self.bar_count,
            "freshness": self.freshness,
            "age_seconds": self.age_seconds,
        }


@dataclass(frozen=True, slots=True)
class WatchlistSnapshot:
    state: str
    reason: str | None
    provider: str | None
    provider_state: str | None
    timeframe: str
    as_of: datetime | None
    rows: tuple[WatchlistRow, ...]

    def payload(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "provider": self.provider,
            "provider_state": self.provider_state,
            "timeframe": self.timeframe,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "rows": [row.payload() for row in self.rows],
        }


class WatchlistService:
    """Price and indicator rows for one symbol universe.

    Indicator periods are the baseline strategy's own (EMA 20, RSI 14,
    ATR 14), so a watchlist that looks tradeable is evaluated with the same
    numbers the strategy would see.
    """

    def __init__(self, series: CandleSeriesService, symbols, *,
                 timeframe: str | None = None,
                 ema_period: int = 20, rsi_period: int = 14,
                 atr_period: int = 14):
        if ema_period < 1 or rsi_period < 1 or atr_period < 1:
            raise ValueError("indicator periods must be positive")
        self._series = series
        self._symbols = tuple(symbols)
        self._timeframe = timeframe
        self._ema_period = ema_period
        self._rsi_period = rsi_period
        self._atr_period = atr_period
        self._lock = threading.RLock()

    def snapshot(self) -> WatchlistSnapshot:
        with self._lock:
            symbols = self._symbols
            frame = self._timeframe
        settings = self._series.settings
        if not self._series.configured:
            reason = str(settings.unavailable_reason or "no market data provider is connected")
            return WatchlistSnapshot(
                state=NOT_CONFIGURED, reason=reason, provider=settings.provider,
                provider_state=NOT_CONFIGURED,
                timeframe=frame or settings.timeframe, as_of=None,
                rows=tuple(self._unavailable_row(symbol, reason) for symbol in symbols))

        frame = frame or settings.timeframe
        rows = []
        provider_state: str | None = None
        as_of: datetime | None = None
        for symbol in symbols:
            row, observed_state = self._row(symbol, frame)
            rows.append(row)
            if observed_state is not None:
                provider_state = observed_state
            latest = row.last_price_at
            if latest is not None and (as_of is None or latest > as_of):
                as_of = latest
        priced = sum(1 for row in rows if row.state == PRICED)
        return WatchlistSnapshot(
            state=OK if priced else NO_DATA,
            reason=None if priced else
            "no symbol in the universe returned usable market data",
            provider=settings.provider, provider_state=provider_state,
            timeframe=frame, as_of=as_of, rows=tuple(rows))

    def _unavailable_row(self, symbol: str, reason: str) -> WatchlistRow:
        return WatchlistRow(
            symbol=symbol, state=UNPRICED, reason=reason,
            last_price=None, last_price_at=None, previous_close=None,
            change_percent=None, volume=None, ema=None, rsi=None, atr=None,
            indicator_state=INDICATORS_UNAVAILABLE, indicator_reason=reason,
            bar_count=0, freshness=None, age_seconds=None)

    def _row(self, symbol: str, timeframe: str) -> tuple[WatchlistRow, str | None]:
        try:
            series = self._series.get(symbol, timeframe=timeframe)
        except ValueError as error:
            return self._unavailable_row(symbol, str(error)), None
        provider_state = series.provider_state
        if series.state is not OK:
            reason = series.reason or f"market data is {series.state}"
            return self._unavailable_row(symbol, reason), provider_state
        bars = series.bars
        last = bars[-1]
        previous_close = bars[-2].close if len(bars) > 1 else None
        change = None
        if previous_close not in (None, 0):
            change = (last.close - previous_close) / previous_close * 100.0
        required = max(self._ema_period, self._rsi_period + 1, self._atr_period + 1)
        if len(bars) >= required:
            closes = [bar.close for bar in bars]
            indicator_state = INDICATORS_OK
            indicator_reason = None
            ema_value = ema(closes, self._ema_period)
            rsi_value = rsi(closes, self._rsi_period)
            atr_value = atr(bars, self._atr_period)
        else:
            indicator_state = INDICATORS_INSUFFICIENT
            indicator_reason = (f"need at least {required} valid candles for EMA/RSI/ATR, "
                                f"received {len(bars)}")
            ema_value = rsi_value = atr_value = None
        return WatchlistRow(
            symbol=symbol, state=PRICED, reason=None,
            last_price=last.close, last_price_at=last.timestamp,
            previous_close=previous_close, change_percent=change,
            volume=last.volume, ema=ema_value, rsi=rsi_value, atr=atr_value,
            indicator_state=indicator_state, indicator_reason=indicator_reason,
            bar_count=len(bars), freshness=series.freshness,
            age_seconds=series.age_seconds), provider_state

    @property
    def timeframe(self) -> str | None:
        return self._timeframe
