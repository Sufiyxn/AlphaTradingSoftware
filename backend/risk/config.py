from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Deterministic, configuration-driven risk limits.

    Every production risk decision is validated against these values before an
    order may exist. Configuration may only TIGHTEN a shipped safety limit
    (see the ``MAX_*`` ceilings and ``MIN_REWARD_RISK`` floor); an environment
    value that would widen a reviewed default is rejected at startup instead
    of silently making the platform riskier.

    Sizing formula (see ``backend.risk.sizing``)::

        risk_budget    = min(available_capital * risk_fraction,
                             equity * max_risk_per_trade,
                             max_risk_amount when set)
        risk_quantity  = risk_budget // |entry - stop|
        safe_quantity  = min(risk_quantity, exposure_quantity, cash_quantity,
                             notional_quantity, symbol_quantity, max_quantity)
        approved       = min(safe_quantity, requested_quantity)

    The approved quantity is never increased, never exceeds a configured
    limit, and a safe quantity of zero rejects the trade. The formula is a
    deterministic constraint solver - it does not and cannot guarantee
    profitability.
    """

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
    # Phase 8 hardening limits.
    max_quantity: int = 10_000
    max_symbol_exposure: float = 25_000.0
    max_entry_price: float = 100.0
    require_fresh_valuation: bool = False

    # Absolute ceilings. Configuration may tighten these but can never widen a
    # shipped safety limit, so an environment file cannot quietly make the
    # platform riskier than the reviewed defaults.
    MAX_RISK_FRACTION = 0.05
    MAX_RISK_PER_TRADE_FRACTION = 0.05
    MAX_EXPOSURE_FRACTION = 1.0
    MAX_DAILY_LOSS_FRACTION = 0.10
    MAX_SIMULTANEOUS_POSITIONS = 50
    MIN_REWARD_RISK = 2.0
    MAX_QUANTITY_CEILING = 100_000
    MAX_ENTRY_PRICE_CEILING = 100.0

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
        if not isinstance(self.max_quantity, int) or isinstance(self.max_quantity, bool) \
                or not 1 <= self.max_quantity <= self.MAX_QUANTITY_CEILING:
            raise ValueError(
                f"max_quantity must be an integer in [1, {self.MAX_QUANTITY_CEILING}]")
        if self.max_symbol_exposure <= 0:
            raise ValueError("max_symbol_exposure must be positive")
        if not 0 < self.max_entry_price <= self.MAX_ENTRY_PRICE_CEILING:
            raise ValueError(
                f"max_entry_price must be in (0, {self.MAX_ENTRY_PRICE_CEILING}]")

    @property
    def limits(self) -> tuple[tuple[str, float | int | bool], ...]:
        """The active limits, for decision auditability and status reporting."""
        return (
            ("risk_fraction", self.risk_fraction),
            ("max_risk_per_trade", self.max_risk_per_trade),
            ("max_risk_amount", self.max_risk_amount if self.max_risk_amount is not None else 0.0),
            ("max_position_notional", self.max_position_notional),
            ("max_portfolio_exposure_fraction", self.max_portfolio_exposure_fraction),
            ("max_simultaneous_positions", self.max_simultaneous_positions),
            ("max_daily_loss_fraction", self.max_daily_loss_fraction),
            ("min_reward_risk", self.min_reward_risk),
            ("max_quantity", self.max_quantity),
            ("max_symbol_exposure", self.max_symbol_exposure),
            ("max_entry_price", self.max_entry_price),
            ("allow_duplicate_symbol", self.allow_duplicate_symbol),
            ("require_fresh_valuation", self.require_fresh_valuation),
        )
