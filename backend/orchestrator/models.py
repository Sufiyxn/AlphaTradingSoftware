from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from backend.ai.models import SetupAnalysis
from backend.paper_trading.models import PaperTradeRecord
from backend.scanner.models import Opportunity


class OrchestratorStatus(str, Enum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    PAUSED = "PAUSED"
    EMERGENCY_STOPPED = "EMERGENCY_STOPPED"


@dataclass(frozen=True, slots=True)
class OrchestratorSnapshot:
    status: OrchestratorStatus
    last_scan_timestamp: datetime | None
    current_cycle: int
    candidates_found: int
    candidates_rejected: int
    trades_approved: int
    trades_rejected_by_risk: int
    active_positions: int
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CycleResult:
    cycle: int
    timestamp: datetime
    candidates: tuple[Opportunity, ...]
    approved_trades: tuple[PaperTradeRecord, ...]
    rejected_count: int
    errors: tuple[str, ...]
    ai_analyses: tuple[SetupAnalysis, ...] = ()
