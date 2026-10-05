from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RiskConfig:
    risk_fraction: float = 0.01
    max_risk_per_trade: float = 0.01
    max_risk_amount: float | None = None
    max_position_notional: float = 25_000.0
    max_portfolio_exposure_fraction: float = 0.50
    max_simultaneous_positions: int = 5
    max_daily_loss_fraction: float = 0.03
    min_reward_risk: float = 2.0
    allow_duplicate_symbol: bool = False
    enabled: bool = True

    def __post_init__(self) -> None:
        for field in ("risk_fraction", "max_risk_per_trade", "max_portfolio_exposure_fraction", "max_daily_loss_fraction"):
            if not 0 < getattr(self, field) <= 1:
                raise ValueError(f"{field} must be in (0, 1]")
        if self.max_position_notional <= 0 or self.max_simultaneous_positions < 1:
            raise ValueError("position and simultaneous-position limits must be positive")
        if self.max_risk_amount is not None and self.max_risk_amount <= 0:
            raise ValueError("max_risk_amount must be positive when set")
        if self.min_reward_risk < 2.0:
            raise ValueError("min_reward_risk cannot be below 2.0")
