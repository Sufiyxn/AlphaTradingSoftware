from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    starting_capital: float = 100_000.0
    risk_fraction: float = 0.01
    max_exposure_fraction: float = 0.25
    max_simultaneous_positions: int = 5
    max_trades_per_symbol: int = 1
    commission_per_share: float = 0.005
    slippage_per_share: float = 0.01
    min_reward_risk: float = 2.0
    qualification_win_rate: float = 0.51
    session_open: time = time(9, 30)
    session_close: time = time(16, 0)
    timezone_name: str = "US/Eastern"

    def __post_init__(self) -> None:
        if self.starting_capital <= 0:
            raise ValueError("starting_capital must be positive")
        if not 0 < self.risk_fraction <= 1 or not 0 < self.max_exposure_fraction <= 1:
            raise ValueError("risk and exposure fractions must be in (0, 1]")
        if self.max_simultaneous_positions < 1:
            raise ValueError("max_simultaneous_positions must be >= 1")
        if self.max_trades_per_symbol < 1:
            raise ValueError("max_trades_per_symbol must be >= 1")
        if self.commission_per_share < 0 or self.slippage_per_share < 0:
            raise ValueError("fees and slippage cannot be negative")
        if self.min_reward_risk < 2.0:
            raise ValueError("min_reward_risk cannot be below 2.0")
        if not 0 <= self.qualification_win_rate <= 1:
            raise ValueError("qualification_win_rate must be between 0 and 1")
        if self.session_open >= self.session_close:
            raise ValueError("session_open must be earlier than session_close")
