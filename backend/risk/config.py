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

    # Absolute ceilings. Configuration may tighten these but can never widen a
    # shipped safety limit, so an environment file cannot quietly make the
    # platform riskier than the reviewed defaults.
    MAX_RISK_FRACTION = 0.05
    MAX_RISK_PER_TRADE_FRACTION = 0.05
    MAX_EXPOSURE_FRACTION = 1.0
    MAX_DAILY_LOSS_FRACTION = 0.10
    MAX_SIMULTANEOUS_POSITIONS = 50
    MIN_REWARD_RISK = 2.0

    def __post_init__(self) -> None:
        for field in ("risk_fraction", "max_risk_per_trade", "max_portfolio_exposure_fraction", "max_daily_loss_fraction"):
            if not 0 < getattr(self, field) <= 1:
                raise ValueError(f"{field} must be in (0, 1]")
        if self.risk_fraction > self.MAX_RISK_FRACTION:
            raise ValueError(f"risk_fraction cannot exceed {self.MAX_RISK_FRACTION}")
        if self.max_risk_per_trade > self.MAX_RISK_PER_TRADE_FRACTION:
            raise ValueError(f"max_risk_per_trade cannot exceed {self.MAX_RISK_PER_TRADE_FRACTION}")
        if self.max_portfolio_exposure_fraction > self.MAX_EXPOSURE_FRACTION:
            raise ValueError(f"max_portfolio_exposure_fraction cannot exceed {self.MAX_EXPOSURE_FRACTION}")
        if self.max_daily_loss_fraction > self.MAX_DAILY_LOSS_FRACTION:
            raise ValueError(f"max_daily_loss_fraction cannot exceed {self.MAX_DAILY_LOSS_FRACTION}")
        if self.max_position_notional <= 0 or not 1 <= self.max_simultaneous_positions <= self.MAX_SIMULTANEOUS_POSITIONS:
            raise ValueError(
                f"position limits must be positive and at most {self.MAX_SIMULTANEOUS_POSITIONS} simultaneous positions")
        if self.max_risk_amount is not None and self.max_risk_amount <= 0:
            raise ValueError("max_risk_amount must be positive when set")
        if self.min_reward_risk < self.MIN_REWARD_RISK:
            raise ValueError(f"min_reward_risk cannot be below {self.MIN_REWARD_RISK}")
