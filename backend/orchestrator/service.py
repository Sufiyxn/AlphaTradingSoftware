import logging
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from backend.ai import AIAnalysisService
from backend.database import JournalRepository
from backend.market_data import DataValidationError, MarketDataProviderError, MarketDataService
from backend.paper_trading import PaperTradingEngine
from backend.portfolio import Portfolio
from backend.risk import RiskEngine
from backend.scanner import MarketScanner
from backend.strategies import StrategyEngine

from .config import OrchestratorConfig
from .models import CycleResult, OrchestratorSnapshot, OrchestratorStatus


logger = logging.getLogger(__name__)


class TradingOrchestrator:
    """Coordinate scanning, strategy evaluation, risk, and paper execution."""

    def __init__(self, *, market_data: MarketDataService, scanner: MarketScanner,
                 strategy: StrategyEngine, risk_engine: RiskEngine,
                 paper_trading: PaperTradingEngine, portfolio: Portfolio,
                 config: OrchestratorConfig, clock=lambda: datetime.now(timezone.utc),
                 ai_analysis: AIAnalysisService | None = None,
                 journal: JournalRepository | None = None):
        self.market_data = market_data
        self.scanner = scanner
        self.strategy = strategy
        self.risk_engine = risk_engine
        self.paper_trading = paper_trading
        self.portfolio = portfolio
        self.config = config
        self.ai_analysis = ai_analysis
        self.journal = journal
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
        self._errors: list[str] = []

    @property
    def state(self) -> OrchestratorSnapshot:
        with self._lock:
            return OrchestratorSnapshot(
                self._status, self._last_scan, self._cycle, self._candidates_found,
                self._candidates_rejected, self._approved, self._risk_rejected,
                len(self.portfolio.positions), tuple(self._errors),
            )

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
        errors: list[str] = []
        approved = []
        analyses = []
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
            else:
                candidates = tuple(self.scanner.scan(self.config.universe))
        except Exception as error:
            message = f"cycle {cycle}: scanner failed: {error}"
            logger.exception(message)
            self._record_error(message)
            candidates = ()
            errors.append(message)

        for candidate in candidates:
            if self._halted():
                break
            symbol = candidate.symbol
            self._journal_event("SCANNER_CANDIDATE", f"Scanned candidate for {symbol}",
                                {"cycle": cycle, "candidate": candidate})
            try:
                if not candidate.valid:
                    rejected += 1
                    self._journal_event("CANDIDATE_REJECTED", f"Invalid scanner candidate for {symbol}",
                                        {"candidate": candidate})
                    continue
                if candidate.entry_price is None or candidate.entry_price >= self.config.max_candidate_price:
                    rejected += 1
                    self._journal_event("CANDIDATE_REJECTED", f"Candidate price is outside the approved range for {symbol}",
                                        {"entry_price": candidate.entry_price,
                                         "maximum_exclusive": self.config.max_candidate_price})
                    continue
                end = candidate.timestamp
                bars = self.market_data.get_historical(symbol, end - self.config.history_lookback, end + timedelta(microseconds=1))
                if not bars:
                    raise DataValidationError("no market history available for strategy evaluation")
                self._journal_event("MARKET_CONTEXT_RETRIEVED", f"Loaded strategy market context for {symbol}",
                                    {"cycle": cycle, "symbol": symbol,
                                     "bar_count": len(bars), "latest_bar": bars[-1]})
                signal = self.strategy.evaluate(bars)
                self._journal_call("record_strategy_signal", signal)
                setup = signal.setup
                if setup is None:
                    rejected += 1
                    continue
                if setup.entry_price >= self.config.max_candidate_price:
                    rejected += 1
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
                if self._halted():
                    break
                decision = self.risk_engine.evaluate(setup, self.portfolio.snapshot())
                self._journal_call("record_risk_decision", decision)
                if not decision.approved:
                    rejected += 1
                    with self._lock:
                        self._risk_rejected += 1
                    continue
                with self._lock:
                    if self._emergency_stopped or self._terminated or self._status is OrchestratorStatus.PAUSED:
                        break
                    result = self.paper_trading.submit_decision(decision, analysis=analysis)
                if result.accepted and result.trade is not None:
                    approved.append(result.trade)
                    with self._lock:
                        self._approved += 1
                elif result.order is not None:
                    rejected += 1
                    reason = f": {result.order.rejection_reason}" if result.order.rejection_reason else ""
                    message = f"{symbol}: paper execution did not fill order {result.order.order_id} ({result.order.status}{reason})"
                    errors.append(message)
                    self._record_error(message)
                else:
                    rejected += 1
                    with self._lock:
                        self._risk_rejected += 1
            except (DataValidationError, MarketDataProviderError, ValueError) as error:
                rejected += 1
                message = f"{symbol}: candidate processing failed: {error}"
                errors.append(message)
                self._record_error(message)
                logger.warning(message)
            except Exception as error:
                rejected += 1
                message = f"{symbol}: candidate processing failed: {error}"
                logger.exception(message)
                errors.append(message)
                self._record_error(message)

        with self._lock:
            self._last_scan = timestamp
            self._candidates_found += len(candidates)
            self._candidates_rejected += rejected
        self._journal_event("CYCLE_COMPLETED", f"Trading cycle {cycle} completed",
                            {"cycle": cycle, "candidates": len(candidates), "approved": len(approved),
                             "rejected": rejected})
        return CycleResult(cycle, timestamp, candidates, tuple(approved), rejected, tuple(errors), tuple(analyses))

    def _halted(self) -> bool:
        with self._lock:
            return self._emergency_stopped or self._terminated or self._status is OrchestratorStatus.PAUSED

    def _journal_call(self, method: str, *args, **kwargs) -> None:
        if self.journal is None:
            return
        try:
            getattr(self.journal, method)(*args, **kwargs)
        except Exception:
            logger.exception("Unable to persist journal entry using %s", method)

    def _journal_event(self, event: str, message: str, context: dict | None = None) -> None:
        self._journal_call("record_event", self._clock(), event, message, context=context)

    def _record_error(self, message: str, *, symbol: str | None = None) -> None:
        with self._lock:
            self._errors.append(message)
        logger.error(message)
        if self.journal is not None:
            try:
                self.journal.record_error(self._clock(), message, symbol=symbol)
            except Exception:
                logger.exception("Unable to persist orchestrator error")
