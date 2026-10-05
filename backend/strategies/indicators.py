from collections.abc import Sequence

from backend.market_data.models import Bar


def ema(values: Sequence[float], period: int) -> float:
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(values) < period:
        raise ValueError("insufficient values for EMA")
    alpha = 2.0 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def rsi(values: Sequence[float], period: int) -> float:
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(values) < period + 1:
        raise ValueError("insufficient values for RSI")
    changes = [current - previous for previous, current in zip(values, values[1:])]
    gains = sum(max(change, 0.0) for change in changes[:period]) / period
    losses = sum(max(-change, 0.0) for change in changes[:period]) / period
    for change in changes[period:]:
        gains = (gains * (period - 1) + max(change, 0.0)) / period
        losses = (losses * (period - 1) + max(-change, 0.0)) / period
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def atr(bars: Sequence[Bar], period: int) -> float:
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(bars) < period + 1:
        raise ValueError("insufficient bars for ATR")
    true_ranges = []
    for previous, current in zip(bars, bars[1:]):
        true_ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    result = sum(true_ranges[:period]) / period
    for value in true_ranges[period:]:
        result = (result * (period - 1) + value) / period
    return result
