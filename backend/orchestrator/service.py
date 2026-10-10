import logging
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from backend.ai import AIAnalysisService
from backend.database import JournalRepository
from backend.market_data import DataValidationError, MarketDataProviderError, MarketDataService
from backend.market_data.session import (
    DEFAULT_SESSION_POLICY,
    AssetClass,
    normalize_session_policy,
    session_gate,
)
from backend.paper_trading import PaperTradingEngine
from backend.pipeline import ExecutionIntent
from backend.portfolio import Portfolio
from backend.risk import RiskEngine
from backend.scanner import MarketScanner
from backend.strategies import StrategyEngine

from .config import OrchestratorConfig
from .decisions import (
    ADVISORY,
    APPROVED,
    CANDIDATE,
    CYCLE,
    DATA_FRESHNESS,
    DECISION_EVENT,
    EXECUTION,
    REJECTED,
    RISK,
    SCANNER,
    SESSION,
    SKIPPED,
    STRATEGY,
    DecisionLedger,
    DecisionRecord,
)
from .models import CycleResult, OrchestratorSnapshot, OrchestratorStatus


logger = logging.getLogger(__name__)


def _latest_cycle(rows, event_type: str) -> int | None:
    """The newest journaled cycle number for one marker event type."""
    for row in rows:
        if row.get("event_type") != event_type:
            continue
        context = row.get("context") or row.get("context_json") or {}
        if not isinstance(context, dict):
            continue
        try:
            return int(context.get("cycle"))
        except (TypeError, ValueError):
            continue
    return None


class TradingOrchestrator:
    """Coordinate scanning, strategy evaluation, risk, and paper execution."""

    def __init__(self, *, market_data: MarketDataService, scanner: MarketScanner,
                 strategy: StrategyEngine, risk_engine: RiskEngine,
                 paper_trading: PaperTradingEngine, portfolio: Portfolio,
                 config: OrchestratorConfig, clock=lambda: datetime.now(timezone.utc),
                 ai_analysis: AIAnalysisService | None = None,
                 journal: JournalRepository | None = None,
                 session_submission: str = DEFAULT_SESSION_POLICY,
                 provider_extended_hours=None):
        self.market_data = market_data
        self.scanner = scanner
        self.strategy = strategy
        self.risk_engine = risk_engine
        self.paper_trading = paper_trading
        self.portfolio = portfolio
        self.config = config
        self.ai_analysis = ai_analysis
        self.journal = journal
        #: Phase 11 checkpoint 2: which market sessions the scanner may
        #: evaluate candidates in. Enforcement of actual submissions stays
        #: with the pipeline; this only avoids scanning when no entry could
        #: be acted on. ``provider_extended_hours`` is a callable reporting
        #: the connected execution provider's extended-hours capability; it
        #: is read fail-closed when omitted or when it fails.
        self.session_submission = normalize_session_policy(session_submission)
        self._provider_extended_hours = provider_extended_hours
        if journal is not None:
            if self.paper_trading.journal is not None and self.paper_trading.journal is not journal:
                raise ValueError("orchestrator and paper trading must share one journal repository")
            self.paper_trading.journal = journal
        self._clock = clock
        self._lock = threading.RLock()
        self._cycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._loop_active = False
        self._emergency_stopped = False
        self._terminated = False
        self._status = OrchestratorStatus.STOPPED
        self._last_scan: datetime | None = None
        self._cycle = 0
        self._candidates_found = 0
        self._candidates_rejected = 0
        self._approved = 0
        self._risk_rejected = 0
        self._data_stale_rejected = 0
        self._interrupted_cycles = 0
        self._errors: list[str] = []
        self._session_blocked = False
        self._session_reason: str | None = None
        self._ledger = DecisionLedger()
        #: Phase 12 checkpoint 1: resume where the journal says the engine
        #: stopped, so a restart neither renumbers past cycles nor loses the
        #: decisions the interrupted cycle already made.
        self._recover_state()

    @property
    def state(self) -> OrchestratorSnapshot:
        with self._lock:
            summary = self._ledger.summary()
            return OrchestratorSnapshot(
                self._status, self._last_scan, self._cycle, self._candidates_found,
                self._candidates_rejected, self._approved, self._risk_rejected,
                len(self.portfolio.positions), tuple(self._errors),
                self._session_blocked, self._session_reason,
                self._data_stale_rejected, self._interrupted_cycles,
                summary["total"], summary["latest"],
            )

    def decisions(self, limit: int = 100) -> dict:
        """Recent explainable decisions and the engine's summary.

        The ledger is rebuilt from the journal on start, so this survives a
        restart; ``summary`` counts what the ledger currently holds.
        """
        records = self._ledger.recent(limit)
        return {"summary": self._ledger.summary(),
                "decisions": [record.payload() for record in records]}

    def start(self) -> None:
        with self._lock:
            if self._emergency_stopped or self._terminated or self._loop_active:
                return
            self._loop_active = True
            self._status = OrchestratorStatus.RUNNING
            self._stop_event.clear()
        logger.info("Trading orchestrator started")
        self._journal_event("ORCHESTRATOR_STARTED", "Trading orchestrator started")
        try:
            while not self._stop_event.is_set():
                if self.state.status is OrchestratorStatus.RUNNING:
                    self.run_once()
                self._stop_event.wait(self.config.scan_interval.total_seconds())
        finally:
            with self._lock:
                self._loop_active = False
                if self._status not in (OrchestratorStatus.PAUSED, OrchestratorStatus.EMERGENCY_STOPPED):
                    self._status = OrchestratorStatus.STOPPED
            logger.info("Trading orchestrator stopped")
            self._journal_event("ORCHESTRATOR_STOPPED", "Trading orchestrator stopped")

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            if not self._emergency_stopped:
                self._status = OrchestratorStatus.STOPPED
        logger.info("Trading orchestrator stop requested")
        self._journal_event("ORCHESTRATOR_STOP_REQUESTED", "Trading orchestrator stop requested")

    def pause(self) -> None:
        with self._lock:
            if self._emergency_stopped or self._terminated:
                raise RuntimeError("orchestrator is stopped and cannot be paused")
            self._status = OrchestratorStatus.PAUSED
        self._journal_event("ORCHESTRATOR_PAUSED", "Trading orchestrator paused")

    def resume(self) -> None:
        with self._lock:
            if self._emergency_stopped or self._terminated:
                raise RuntimeError("orchestrator cannot resume after emergency stop or termination")
            self._status = OrchestratorStatus.RUNNING
            self._stop_event.clear()
        self._journal_event("ORCHESTRATOR_RESUMED", "Trading orchestrator resumed")

    def emergency_stop(self, *, triggered_by: str, reason: str = "emergency stop") -> None:
        with self._lock:
            self._emergency_stopped = True
            self._status = OrchestratorStatus.EMERGENCY_STOPPED
            self._stop_event.set()
        self.risk_engine.set_kill_switch(True)
        self._journal_event("EMERGENCY_STOP", reason,
                            {"triggered_by": triggered_by, "reason": reason})

    def reset_emergency_stop(self, *, triggered_by: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("a reset reason is required")
        with self._lock:
            if not self._emergency_stopped:
                raise RuntimeError("emergency stop is not active")
            self._emergency_stopped = False
            self._status = OrchestratorStatus.STOPPED
            self._stop_event.set()
        self.risk_engine.set_kill_switch(False)
        self._journal_event("EMERGENCY_STOP_RESET", "Owner reset emergency stop",
                            {"triggered_by": triggered_by, "reason": reason})

    def terminate(self, *, triggered_by: str) -> None:
        with self._lock:
            self._terminated = True
            if not self._emergency_stopped:
                self._status = OrchestratorStatus.STOPPED
            self._stop_event.set()
        self._journal_event("APPLICATION_TERMINATION_REQUESTED", "Graceful termination requested",
                            {"triggered_by": triggered_by})

    def update_runtime_config(self, **changes) -> None:
        allowed = {"scan_interval", "history_lookback", "max_candidate_price"}
        if not changes or not set(changes).issubset(allowed):
            raise ValueError("runtime configuration contains unsupported settings")
        if "max_candidate_price" in changes and not 0 < changes["max_candidate_price"] <= 100:
            raise ValueError("candidate price limit must be positive and cannot exceed $100")
        with self._lock:
            self.config = replace(self.config, **changes)
        self._journal_event("RUNTIME_CONFIG_UPDATED", "Approved runtime configuration updated", changes)

    def run_once(self) -> CycleResult:
        """Perform one complete scan/decision cycle; never starts a loop."""
        with self._cycle_lock:
            return self._run_cycle()

    def _run_cycle(self) -> CycleResult:
        timestamp = self._clock()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("orchestrator clock must return a timezone-aware datetime")
        with self._lock:
            if self._status in (OrchestratorStatus.PAUSED, OrchestratorStatus.EMERGENCY_STOPPED) or self._terminated:
                return CycleResult(self._cycle, timestamp, (), (), 0, ())
            self._cycle += 1
            cycle = self._cycle
        self._journal_event("CYCLE_STARTED", f"Trading cycle {cycle} started", {"cycle": cycle})

        # Phase 11 checkpoint 2: outside the allowed market session no entry
        # could be acted on, so the scan is skipped entirely - no market-data
        # fetch, no strategy evaluation, no AI advisory, no submission
        # attempt. The refusal is journaled with the calendar's reason and is
        # never counted as an error: a closed market is a normal state.
        session = session_gate(
            timestamp, policy=self.session_submission,
            provider_extended_hours=self._extended_hours(),
            asset_class=AssetClass.US_EQUITY)
        with self._lock:
            self._session_blocked = not session.allowed
            self._session_reason = session.reason if not session.allowed else None
        if not session.allowed:
            self._journal_event(
                "CYCLE_SESSION_SKIPPED",
                f"Trading cycle {cycle} skipped: {session.reason}",
                {"cycle": cycle, "code": session.code, "reason": session.reason,
                 "policy": session.policy, "session_state": session.state,
                 "trading_day": session.is_trading_day})
            decision = self._record_decision(
                cycle, timestamp, None, SESSION, SKIPPED, (session.reason,),
                {"code": session.code, "policy": session.policy,
                 "session_state": session.state})
            with self._lock:
                self._last_scan = timestamp
            self._journal_event(
                "CYCLE_COMPLETED", f"Trading cycle {cycle} completed",
                {"cycle": cycle, "candidates": 0, "approved": 0, "rejected": 0,
                 "session_skipped": True, "decisions": 1})
            return CycleResult(cycle, timestamp, (), (), 0, (),
                               session_skipped=True, decisions=(decision,))

        errors: list[str] = []
        approved = []
        analyses = []
        decisions: list[DecisionRecord] = []
        rejected = 0
        try:
            detailed_scan = getattr(self.scanner, "scan_detailed", None)
            if callable(detailed_scan):
                scan_result, symbol_results = detailed_scan(self.config.universe)
                candidates = tuple(scan_result)
                for symbol_result in symbol_results:
                    if symbol_result.error:
                        message = f"{symbol_result.symbol}: scanner data failure: {symbol_result.error}"
                        errors.append(message)
                        rejected += 1
                        self._record_error(message)
                        logger.warning(message)
                        decisions.append(self._record_decision(
                            cycle, timestamp, symbol_result.symbol, SCANNER, REJECTED,
                            (symbol_result.error,), {"source": "scanner"}))
            else:
                candidates = tuple(self.scanner.scan(self.config.universe))
        except Exception as error:
            message = f"cycle {cycle}: scanner failed: {error}"
            logger.exception(message)
            self._record_error(message)
            candidates = ()
            errors.append(message)
            rejected += 1
            decisions.append(self._record_decision(
                cycle, timestamp, None, SCANNER, REJECTED,
                (f"scanner failed: {error}",), {"source": "scanner"}))

        for candidate in candidates:
            if self._halted():
                decisions.append(self._record_decision(
                    cycle, timestamp, None, CYCLE, SKIPPED,
                    ("cycle halted before every candidate was evaluated",)))
                break
            symbol = candidate.symbol
            self._journal_event("SCANNER_CANDIDATE", f"Scanned candidate for {symbol}",
                                {"cycle": cycle, "candidate": candidate})
            try:
                if not candidate.valid:
                    rejected += 1
                    self._journal_event("CANDIDATE_REJECTED", f"Invalid scanner candidate for {symbol}",
                                        {"candidate": candidate})
                    decisions.append(self._record_decision(
                        cycle, timestamp, symbol, CANDIDATE, REJECTED,
                        tuple(candidate.reasons) or ("scanner candidate was not valid",),
                        {"source": "scanner"}))
                    continue
                if candidate.entry_price is None or candidate.entry_price >= self.config.max_candidate_price:
                    rejected += 1
                    self._journal_event("CANDIDATE_REJECTED", f"Candidate price is outside the approved range for {symbol}",
                                        {"entry_price": candidate.entry_price,
                                         "maximum_exclusive": self.config.max_candidate_price})
                    decisions.append(self._record_decision(
                        cycle, timestamp, symbol, CANDIDATE, REJECTED,
                        (f"entry price {candidate.entry_price} is outside the approved range",),
                        {"entry_price": candidate.entry_price,
                         "maximum_exclusive": self.config.max_candidate_price}))
                    continue
                end = candidate.timestamp
                bars = self.market_data.get_historical(symbol, end - self.config.history_lookback, end + timedelta(microseconds=1))
                if not bars:
                    raise DataValidationError("no market history available for strategy evaluation")
                self._journal_event("MARKET_CONTEXT_RETRIEVED", f"Loaded strategy market context for {symbol}",
                                    {"cycle": cycle, "symbol": symbol,
                                     "bar_count": len(bars), "latest_bar": bars[-1]})
                # Phase 12 checkpoint 1: a candidate is never evaluated, and
                # certainly never submitted, against bars older than the
                # freshness limit. The age is measured against the bar the
                # provider actually returned - the engine does not guess a
                # price it does not have.
                age_seconds = (timestamp - bars[-1].timestamp).total_seconds()
                age_limit = self.config.data_age_limit
                if age_limit is not None and age_seconds > age_limit:
                    rejected += 1
                    with self._lock:
                        self._data_stale_rejected += 1
                    decisions.append(self._record_decision(
                        cycle, timestamp, symbol, DATA_FRESHNESS, REJECTED,
                        (f"latest bar is {age_seconds:.0f}s old, beyond the "
                         f"{age_limit:.0f}s freshness limit",),
                        {"age_seconds": age_seconds,
                         "limit_seconds": age_limit,
                         "latest_bar": bars[-1].timestamp.isoformat()}))
                    continue
                signal = self.strategy.evaluate(bars)
                self._journal_call("record_strategy_signal", signal)
                setup = signal.setup
                if setup is None:
                    rejected += 1
                    decisions.append(self._record_decision(
                        cycle, timestamp, symbol, STRATEGY, REJECTED,
                        (signal.reason or "strategy produced no trade setup",),
                        {"strategy": signal.strategy_name}))
                    continue
                if setup.entry_price >= self.config.max_candidate_price:
                    rejected += 1
                    decisions.append(self._record_decision(
                        cycle, timestamp, symbol, STRATEGY, REJECTED,
                        (f"setup entry price {setup.entry_price} is outside the approved range",),
                        {"entry_price": setup.entry_price,
                         "maximum_exclusive": self.config.max_candidate_price}))
                    continue
                if setup.symbol.strip().upper() != symbol.strip().upper():
                    raise DataValidationError(f"strategy returned setup for unexpected symbol {setup.symbol}")
                analysis = None
                if self.ai_analysis is not None:
                    try:
                        analysis = self.ai_analysis.analyze(setup, bars)
                        analyses.append(analysis)
                        self._journal_call("record_ai_analysis", analysis)
                    except Exception as error:
                        self._record_error(f"{symbol}: advisory AI analysis unavailable: {error}", symbol=symbol)
                        decisions.append(self._record_decision(
                            cycle, timestamp, symbol, ADVISORY, SKIPPED,
                            (f"advisory AI analysis unavailable: {error}",)))
                if self._halted():
                    break
                # One canonical path: the TradingPipeline evaluates risk
                # against the live portfolio, enforces the execution safety
                # gate and the verified connection, and submits through the
                # selected adapter. The orchestrator only reads the outcome.
                intent = ExecutionIntent(setup=setup, source="orchestrator")
                with self._lock:
                    if self._emergency_stopped or self._terminated or self._status is OrchestratorStatus.PAUSED:
                        break
                    result = self.paper_trading.submit_decision(intent, analysis=analysis)
                if result.accepted and result.trade is not None:
                    approved.append(result.trade)
                    with self._lock:
                        self._approved += 1
                    risk_reasons = tuple(result.risk.reasons) if result.risk is not None else ()
                    decisions.append(self._record_decision(
                        cycle, timestamp, symbol, RISK, APPROVED,
                        risk_reasons or ("risk approved the submission",),
                        {"quantity": getattr(result.risk, "quantity", None)}))
                    order_id = getattr(result.order, "order_id", None)
                    decisions.append(self._record_decision(
                        cycle, timestamp, symbol, EXECUTION, APPROVED,
                        (f"paper order recorded for {result.trade.quantity:g} shares at "
                         f"{result.trade.entry_price:g}",),
                        {"order_id": order_id, "trade_id": getattr(result.trade, "order_id", None),
                         "quantity": result.trade.quantity, "entry_price": result.trade.entry_price}))
                elif result.risk is not None and not result.risk.approved:
                    rejected += 1
                    with self._lock:
                        self._risk_rejected += 1
                    status = getattr(result.risk.status, "value", None)
                    decisions.append(self._record_decision(
                        cycle, timestamp, symbol, RISK, REJECTED,
                        tuple(result.risk.reasons) or ("risk engine rejected the submission",),
                        {"status": status}))
                elif result.order is not None:
                    rejected += 1
                    reason = f": {result.order.rejection_reason}" if result.order.rejection_reason else ""
                    message = f"{symbol}: paper execution did not fill order {result.order.order_id} ({result.order.status}{reason})"
                    errors.append(message)
                    self._record_error(message)
                    decisions.append(self._record_decision(
                        cycle, timestamp, symbol, EXECUTION, REJECTED,
                        (message,),
                        {"order_id": result.order.order_id, "status": str(result.order.status)}))
                else:
                    rejected += 1
                    detail = f": {result.reason}" if result.reason else ""
                    message = f"{symbol}: execution pipeline rejected the submission ({result.code}{detail})"
                    errors.append(message)
                    self._record_error(message)
                    decisions.append(self._record_decision(
                        cycle, timestamp, symbol, EXECUTION, REJECTED,
                        (f"pipeline rejected the submission ({result.code}{detail})",),
                        {"code": result.code}))
            except (DataValidationError, MarketDataProviderError, ValueError) as error:
                rejected += 1
                message = f"{symbol}: candidate processing failed: {error}"
                errors.append(message)
                self._record_error(message)
                logger.warning(message)
                decisions.append(self._record_decision(
                    cycle, timestamp, symbol, CANDIDATE, REJECTED,
                    (f"candidate processing failed: {error}",)))
            except Exception as error:
                rejected += 1
                message = f"{symbol}: candidate processing failed: {error}"
                logger.exception(message)
                errors.append(message)
                self._record_error(message)
                decisions.append(self._record_decision(
                    cycle, timestamp, symbol, CANDIDATE, REJECTED,
                    (f"candidate processing failed: {error}",)))

        with self._lock:
            self._last_scan = timestamp
            self._candidates_found += len(candidates)
            self._candidates_rejected += rejected
        self._journal_event("CYCLE_COMPLETED", f"Trading cycle {cycle} completed",
                            {"cycle": cycle, "candidates": len(candidates), "approved": len(approved),
                             "rejected": rejected, "decisions": len(decisions)})
        return CycleResult(cycle, timestamp, candidates, tuple(approved), rejected,
                           tuple(errors), tuple(analyses), decisions=tuple(decisions))

    def _record_decision(self, cycle: int, timestamp: datetime, symbol: str | None,
                         stage: str, outcome: str, reasons,
                         context: dict | None = None) -> DecisionRecord:
        """One explainable decision: kept in the ledger and journaled."""
        decision = self._ledger.record(cycle, timestamp, symbol, stage, outcome,
                                       reasons, context)
        detail = "; ".join(decision.reasons) if decision.reasons else decision.outcome
        subject = decision.symbol or f"cycle {decision.cycle}"
        self._journal_event(
            DECISION_EVENT,
            f"{subject}: {decision.stage} {decision.outcome} ({detail})",
            {"cycle": decision.cycle, "stage": decision.stage,
             "outcome": decision.outcome, "reasons": list(decision.reasons),
             "context": decision.context},
            symbol=decision.symbol)
        return decision

    def _recover_state(self) -> None:
        """Rebuild cycle numbering and the decision ledger from the journal.

        A restart must not renumber past cycles, must not silently lose a
        cycle that started but never completed, and must not forget the
        decisions already made. A journal that does not support the query is
        treated as empty history, never as an error.
        """
        reader = getattr(self.journal, "events_by_type", None)
        if not callable(reader):
            return
        interrupted: int | None = None
        try:
            markers = reader(("CYCLE_STARTED", "CYCLE_COMPLETED"), limit=100)
        except Exception:
            logger.exception("Unable to recover cycle progress from the journal")
            markers = ()
        if markers:
            started = _latest_cycle(markers, "CYCLE_STARTED")
            completed = _latest_cycle(markers, "CYCLE_COMPLETED")
            if started is not None and (completed is None or started > completed):
                interrupted = started
            with self._lock:
                self._cycle = max(started or 0, completed or 0)
                if interrupted is not None:
                    self._interrupted_cycles += 1
        try:
            rows = reader((DECISION_EVENT,), limit=self._ledger.capacity)
        except Exception:
            logger.exception("Unable to rebuild the decision ledger from the journal")
        else:
            if rows:
                self._ledger.restore(rows)
        if interrupted is not None:
            stamp = self._clock()
            self._journal_event(
                "CYCLE_INTERRUPTED",
                f"Trading cycle {interrupted} started but never completed before a restart",
                {"cycle": interrupted})
            self._record_decision(
                interrupted, stamp, None, CYCLE, SKIPPED,
                ("cycle started but never completed before a restart",),
                {"recovered": True})

    def _halted(self) -> bool:
        with self._lock:
            return self._emergency_stopped or self._terminated or self._status is OrchestratorStatus.PAUSED

    def _extended_hours(self) -> bool:
        """The provider's extended-hours capability, read fail-closed.

        The orchestrator is display-and-scan only; the pipeline enforces the
        same answer at submission time. A provider that cannot be asked (or
        that fails the question) is treated as unsupported, so the scanner
        never widens a session the execution layer would refuse.
        """
        reporter = self._provider_extended_hours
        if reporter is None:
            return False
        try:
            return bool(reporter())
        except Exception:  # noqa: BLE001 - capability bugs must not widen access
            return False

    def _journal_call(self, method: str, *args, **kwargs) -> None:
        if self.journal is None:
            return
        try:
            getattr(self.journal, method)(*args, **kwargs)
        except Exception:
            logger.exception("Unable to persist journal entry using %s", method)

    def _journal_event(self, event: str, message: str, context: dict | None = None,
                       *, symbol: str | None = None) -> None:
        kwargs = {"context": context}
        if symbol:
            kwargs["symbol"] = symbol
        self._journal_call("record_event", self._clock(), event, message, **kwargs)

    def _record_error(self, message: str, *, symbol: str | None = None) -> None:
        with self._lock:
            self._errors.append(message)
        logger.error(message)
        if self.journal is not None:
            try:
                self.journal.record_error(self._clock(), message, symbol=symbol)
            except Exception:
                logger.exception("Unable to persist orchestrator error")
