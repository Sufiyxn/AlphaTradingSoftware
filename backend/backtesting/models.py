from dataclasses import dataclass
from datetime import datetime

from backend.strategies.models import Direction


@dataclass(frozen=True, slots=True)
class SimulatedTrade:
    symbol: str
    direction: Direction
    entry_timestamp: datetime
    entry_price: float
    stop_price: float
    target_price: float
    exit_timestamp: datetime
    exit_price: float
    risk_per_share: float
    reward_per_share: float
    reward_risk_ratio: float
    pnl: float
    pnl_percent: float
    won: bool
    strategy_name: str
    exit_reason: str
    shares: int
    fees: float


@dataclass(frozen=True, slots=True)
class OpenPosition:
    setup: object
    shares: int
    entry_timestamp: datetime
    entry_price: float


@dataclass(frozen=True, slots=True)
class SymbolMetrics:
    trades: int
    wins: int
    win_rate: float
    pnl: float


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    total_return_pct: float
    average_win: float
    average_loss: float
    average_reward_risk: float
    profit_factor: float
    maximum_drawdown: float
    largest_win: float
    largest_loss: float
    consecutive_wins: int
    consecutive_losses: int
    trades_per_symbol: dict[str, int]
    win_rate_per_symbol: dict[str, float]
    pnl_per_symbol: dict[str, float]


@dataclass(frozen=True, slots=True)
class BacktestResult:
    trades: tuple[SimulatedTrade, ...]
    metrics: PerformanceMetrics
    starting_capital: float
    ending_capital: float
    qualified: bool
    completed: bool
    errors: tuple[str, ...]
