from dataclasses import dataclass
from datetime import datetime

from backend.execution.models import Order
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
    risk: RiskDecision
    order: Order | None
    trade: PaperTradeRecord | None

    @property
    def accepted(self) -> bool:
        return self.risk.approved and self.trade is not None
