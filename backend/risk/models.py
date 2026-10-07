from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

from backend.strategies.models import TradeSetup


class RiskStatus(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class RiskCode:
    """Deterministic machine-readable outcome for one risk decision.

    ``code`` always names the FIRST rule that rejected the request, evaluated
    in the fixed order implemented by :class:`backend.risk.RiskEngine`:
    configuration, kill switch, price sanity, direction/stop/target geometry,
    reward:risk, portfolio state (daily loss, positions, duplicates, exposure),
    then sizing/capacity. ``reasons`` carries every rule that failed, in that
    same order, so the decision is fully explainable.
    """

    APPROVED = "APPROVED"
    # configuration and safety
    RISK_DISABLED = "RISK_DISABLED"
    KILL_SWITCH = "KILL_SWITCH"
    # price sanity and trade geometry
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_DIRECTION = "INVALID_DIRECTION"
    STOP_SIDE_INVALID = "STOP_SIDE_INVALID"
    TARGET_SIDE_INVALID = "TARGET_SIDE_INVALID"
    ENTRY_PRICE_LIMIT = "ENTRY_PRICE_LIMIT"
    ZERO_RISK = "ZERO_RISK"
    REWARD_RISK_TOO_LOW = "REWARD_RISK_TOO_LOW"
    # portfolio state
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    MAX_POSITIONS_REACHED = "MAX_POSITIONS_REACHED"
    DUPLICATE_SYMBOL = "DUPLICATE_SYMBOL"
    PORTFOLIO_EXPOSURE_EXCEEDED = "PORTFOLIO_EXPOSURE_EXCEEDED"
    SYMBOL_EXPOSURE_EXCEEDED = "SYMBOL_EXPOSURE_EXCEEDED"
    VALUATION_NOT_FRESH = "VALUATION_NOT_FRESH"
    INVALID_EQUITY = "INVALID_EQUITY"
    # sizing and capacity
    INVALID_REQUEST = "INVALID_REQUEST"
    ZERO_SAFE_QUANTITY = "ZERO_SAFE_QUANTITY"
    MAX_QUANTITY_EXCEEDED = "MAX_QUANTITY_EXCEEDED"
    TRADE_RISK_EXCEEDED = "TRADE_RISK_EXCEEDED"
    POSITION_NOTIONAL_EXCEEDED = "POSITION_NOTIONAL_EXCEEDED"


ALL_RISK_CODES: tuple[str, ...] = tuple(
    value for name, value in vars(RiskCode).items()
    if not name.startswith("_") and isinstance(value, str))


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """One canonical, provider-agnostic risk decision.

    ``quantity`` is the APPROVED quantity and is always ``0`` when the status
    is REJECTED - a rejection never carries an executable size. ``requested_quantity``
    records what the caller asked for (``None`` when the caller did not say);
    approved quantity is ``min(safe quantity, requested quantity)`` and is never
    larger than the request.

    ``code`` names the first rule that rejected the decision (``"APPROVED"``
    when accepted). The remaining fields (prices, exposures, reward:risk,
    limits, timestamp) exist so every decision can be audited from the journal
    alone. No secrets and no fabricated market values are ever stored: prices
    are the caller's setup prices, exposure/equity/daily P&L come from the
    canonical portfolio snapshot, and ``decided_at`` is excluded from equality
    so two evaluations of identical state remain equal.

    ``pending_exposure`` is the notional the risk layer reserved for orders
    that are still open (submitted but not fully filled). It is capacity held
    aside, not an open position, and it disappears from the projection the
    moment those orders reach a terminal state.
    """

    status: RiskStatus
    setup: TradeSetup
    quantity: int | float
    risk_amount: float
    reasons: tuple[str, ...]
    code: str = ""
    requested_quantity: int | float | None = None
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    risk_per_unit: float = 0.0
    reward_per_unit: float = 0.0
    reward_risk: float = 0.0
    current_exposure: float = 0.0
    projected_exposure: float = 0.0
    daily_pnl: float = 0.0
    equity: float = 0.0
    symbol_open: bool = False
    pending_exposure: float = 0.0
    limits: tuple[tuple[str, float | int | bool], ...] = ()
    decided_at: datetime | None = field(default=None, compare=False)

    @property
    def approved(self) -> bool:
        return self.status is RiskStatus.APPROVED
