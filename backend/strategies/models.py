from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    fast_ema_period: int = 20
    slow_ema_period: int = 50
    rsi_period: int = 14
    atr_period: int = 14
    atr_multiplier: float = 2.0
    max_stop_distance_pct: float = 0.02631578947368421
    min_reward_risk: float = 2.0
    bullish_rsi_min: float = 50.0
    bullish_rsi_max: float = 70.0
    bearish_rsi_min: float = 30.0
    bearish_rsi_max: float = 50.0

    def __post_init__(self) -> None:
        for name in ("fast_ema_period", "slow_ema_period", "rsi_period", "atr_period"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.fast_ema_period >= self.slow_ema_period:
            raise ValueError("fast_ema_period must be less than slow_ema_period")
        if self.atr_multiplier <= 0 or self.max_stop_distance_pct <= 0:
            raise ValueError("ATR multiplier and maximum stop distance must be positive")
        if self.min_reward_risk < 2.0:
            raise ValueError("min_reward_risk cannot be below 2.0")
        if not (0 <= self.bullish_rsi_min <= self.bullish_rsi_max <= 100):
            raise ValueError("invalid bullish RSI bounds")
        if not (0 <= self.bearish_rsi_min <= self.bearish_rsi_max <= 100):
            raise ValueError("invalid bearish RSI bounds")


@dataclass(frozen=True, slots=True)
class TradeSetup:
    symbol: str
    direction: Direction
    entry_price: float
    stop_price: float
    target_price: float
    risk_per_share: float
    reward_per_share: float
    reward_risk_ratio: float
    timestamp: datetime
    strategy_name: str
    score: float
    explanation: str


@dataclass(frozen=True, slots=True)
class StrategySignal:
    symbol: str
    timestamp: datetime
    direction: Direction
    setup: TradeSetup | None
    reason: str
    strategy_name: str
