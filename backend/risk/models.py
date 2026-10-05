from dataclasses import dataclass
from enum import Enum

from backend.strategies.models import TradeSetup


class RiskStatus(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    status: RiskStatus
    setup: TradeSetup
    quantity: int
    risk_amount: float
    reasons: tuple[str, ...]

    @property
    def approved(self) -> bool:
        return self.status is RiskStatus.APPROVED
