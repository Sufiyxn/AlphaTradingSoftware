from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PaperTradingConfig:
    commission_per_share: float = 0.005
    slippage_per_share: float = 0.01

    def __post_init__(self) -> None:
        if self.commission_per_share < 0 or self.slippage_per_share < 0:
            raise ValueError("paper fees and slippage cannot be negative")
