from dataclasses import dataclass
from datetime import datetime

from backend.orders.models import Order
from backend.risk.models import RiskDecision
from backend.strategies.models import Direction


@dataclass(frozen=True, slots=True)
class PaperTradeRecord:
    symbol: str
    direction: Direction
    entry_timestamp: datetime
    entry_price: float
    stop_price: float
    target_price: float
    quantity: int
    strategy_name: str
    order_id: str


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """One canonical pipeline outcome, plus the booked paper trade if filled.

    ``risk`` is None only when the pipeline rejected the request before risk
    could be evaluated (invalid input, stale signal, connection failure); it is
    never a fabricated decision. ``code``/``reason`` carry the pipeline's
    deterministic outcome for callers that need to map rejections.
    """

    risk: RiskDecision | None
    order: Order | None
    trade: PaperTradeRecord | None
    code: str | None = None
    reason: str | None = None
    duplicate: bool = False
    connection_id: str | None = None
    provider_id: str | None = None

    @property
    def accepted(self) -> bool:
        return bool(self.risk is not None and self.risk.approved
                    and self.trade is not None and not self.duplicate)
