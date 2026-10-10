from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from backend.ai.models import SetupAnalysis
from backend.paper_trading.models import PaperTradeRecord
from backend.scanner.models import Opportunity

from .decisions import DecisionRecord


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
    #: Phase 11 checkpoint 2: the market-session gate refused this cycle's
    #: scan, so no candidate was evaluated. Not an error - the market being
    #: closed is a normal, expected state.
    session_blocked: bool = False
    session_reason: str | None = None
    #: Phase 12: candidates the data-freshness gate refused before they
    #: could reach the strategy. Stale bars are not an execution error.
    data_stale_rejected: int = 0
    #: Phase 12: cycles that started but never completed before a restart.
    interrupted_cycles: int = 0
    #: Phase 12: size of the explainable decision ledger and its latest entry.
    decisions_total: int = 0
    last_decision: dict | None = None


@dataclass(frozen=True, slots=True)
class CycleResult:
    cycle: int
    timestamp: datetime
    candidates: tuple[Opportunity, ...]
    approved_trades: tuple[PaperTradeRecord, ...]
    rejected_count: int
    errors: tuple[str, ...]
    ai_analyses: tuple[SetupAnalysis, ...] = ()
    #: True when the market-session gate refused the scan for this cycle.
    session_skipped: bool = False
    #: Phase 12: every decision this cycle made, in the order it made them.
    decisions: tuple[DecisionRecord, ...] = ()
