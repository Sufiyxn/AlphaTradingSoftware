import math
from collections.abc import Sequence

from backend.market_data.models import Bar
from backend.market_data.validation import DataValidationError, normalize_bar

from .base import Strategy
from .indicators import atr, ema, rsi
from .models import Direction, StrategyConfig, StrategySignal, TradeSetup


class BaselineTrendMomentumStrategy:
    """Deterministic EMA/RSI baseline for development and backtesting only."""

    name = "baseline_trend_momentum_v1"

    def evaluate(self, bars: Sequence[Bar], config: StrategyConfig) -> StrategySignal:
        valid_bars = self._validate_bars(bars)
        if not valid_bars:
            return self._no_trade(bars, "No market data")

        symbol = valid_bars[-1].symbol
        timestamp = valid_bars[-1].timestamp
        required = max(config.slow_ema_period, config.rsi_period + 1, config.atr_period + 1)
        if len(valid_bars) < required:
            return self._signal(symbol, timestamp, Direction.NONE, None, "Insufficient historical data")

        closes = [bar.close for bar in valid_bars]
        fast = ema(closes, config.fast_ema_period)
        slow = ema(closes, config.slow_ema_period)
        momentum = rsi(closes, config.rsi_period)
        price = closes[-1]

        if fast > slow and price > slow and config.bullish_rsi_min <= momentum <= config.bullish_rsi_max:
            direction = Direction.LONG
        elif fast < slow and price < slow and config.bearish_rsi_min <= momentum <= config.bearish_rsi_max:
            direction = Direction.SHORT
        else:
            return self._signal(symbol, timestamp, Direction.NONE, None, "Trend and momentum conditions do not align")

        entry = price
        # Use ATR through the bar before entry so the current target-confirmation range
        # cannot also inflate the proposed stop distance.
        risk = atr(valid_bars[:-1], config.atr_period) * config.atr_multiplier
        maximum_risk = entry * config.max_stop_distance_pct
        if not math.isfinite(risk) or risk <= 0:
            return self._signal(symbol, timestamp, Direction.NONE, None, "ATR stop distance is invalid")
        if risk > maximum_risk:
            return self._signal(symbol, timestamp, Direction.NONE, None, "ATR stop exceeds configured maximum risk distance")

        signed = 1.0 if direction is Direction.LONG else -1.0
        stop = entry - signed * risk
        minimum_target = entry + signed * risk * config.min_reward_risk
        # Require historical range to have reached the 2R threshold; do not invent a target.
        latest = valid_bars[-1]
        observed_extreme = latest.high if direction is Direction.LONG else latest.low
        target = observed_extreme if (observed_extreme - entry) * signed >= risk * config.min_reward_risk else None
        if target is None:
            return self._signal(symbol, timestamp, Direction.NONE, None, "Observed price range does not support the minimum reward/risk target")

        risk_per_share = abs(entry - stop)
        reward_per_share = abs(target - entry)
        ratio = reward_per_share / risk_per_share
        if ratio < config.min_reward_risk:
            return self._signal(symbol, timestamp, Direction.NONE, None, "Calculated reward/risk is below configured minimum")

        setup = TradeSetup(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            risk_per_share=risk_per_share,
            reward_per_share=reward_per_share,
            reward_risk_ratio=ratio,
            timestamp=timestamp,
            strategy_name=self.name,
            score=abs(fast - slow) / price,
            explanation=f"EMA trend and RSI momentum align; ATR stop; observed range supports target (RSI {momentum:.2f})",
        )
        return self._signal(symbol, timestamp, direction, setup, "Trade setup generated")

    @staticmethod
    def _validate_bars(bars: Sequence[Bar]) -> tuple[Bar, ...]:
        try:
            normalized = tuple(normalize_bar(bar) for bar in bars)
        except (DataValidationError, TypeError) as error:
            raise DataValidationError(f"invalid strategy input: {error}") from error
        if normalized:
            symbol = normalized[0].symbol
            if any(bar.symbol != symbol for bar in normalized):
                raise DataValidationError("strategy input contains multiple symbols")
            if any(current.timestamp <= previous.timestamp for previous, current in zip(normalized, normalized[1:])):
                raise DataValidationError("strategy bars must be strictly chronological")
        return normalized

    def _no_trade(self, bars: Sequence[Bar], reason: str) -> StrategySignal:
        symbol = bars[-1].symbol if bars else ""
        timestamp = bars[-1].timestamp if bars else None
        return self._signal(symbol, timestamp, Direction.NONE, None, reason)

    def _signal(self, symbol, timestamp, direction, setup, reason) -> StrategySignal:
        return StrategySignal(symbol, timestamp, direction, setup, reason, self.name)
