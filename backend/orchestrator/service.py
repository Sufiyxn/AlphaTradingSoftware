import logging
import threading
from datetime import datetime, timedelta, timezone

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
                 config: OrchestratorConfig, clock=lambda: datetime.now(timezone.utc)):
        self.market_data = market_data
        self.scanner = scanner
        self.strategy = strategy
        self.risk_engine = risk_engine
        self.paper_trading = paper_trading
        self.portfolio = portfolio
        self.config = config
        self._clock = clock
        self._lock = threading.RLock()
        self._cycle_lock = threading.Lock()
        self._stop_event = threading.Event()
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
            if self._status is OrchestratorStatus.RUNNING:
                return
            self._status = OrchestratorStatus.RUNNING
            self._stop_event.clear()
        logger.info("Trading orchestrator started")
        try:
            while not self._stop_event.is_set():
                self.run_once()
                self._stop_event.wait(self.config.scan_interval.total_seconds())
        finally:
            with self._lock:
                self._status = OrchestratorStatus.STOPPED
            logger.info("Trading orchestrator stopped")

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            self._status = OrchestratorStatus.STOPPED
        logger.info("Trading orchestrator stop requested")

    def run_once(self) -> CycleResult:
        """Perform one complete scan/decision cycle; never starts a loop."""
        with self._cycle_lock:
            return self._run_cycle()

    def _run_cycle(self) -> CycleResult:
        with self._lock:
            self._cycle += 1
            cycle = self._cycle
        timestamp = self._clock()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("orchestrator clock must return a timezone-aware datetime")
        errors: list[str] = []
        approved = []
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
            symbol = candidate.symbol
            try:
                if not candidate.valid:
                    rejected += 1
                    continue
                if candidate.entry_price is None or candidate.entry_price >= self.config.max_candidate_price:
                    rejected += 1
                    continue
                end = candidate.timestamp
                bars = self.market_data.get_historical(symbol, end - self.config.history_lookback, end + timedelta(microseconds=1))
                if not bars:
                    raise DataValidationError("no market history available for strategy evaluation")
                signal = self.strategy.evaluate(bars)
                setup = signal.setup
                if setup is None:
                    rejected += 1
                    continue
                if setup.entry_price >= self.config.max_candidate_price:
                    rejected += 1
                    continue
                if setup.symbol.strip().upper() != symbol.strip().upper():
                    raise DataValidationError(f"strategy returned setup for unexpected symbol {setup.symbol}")
                decision = self.risk_engine.evaluate(setup, self.portfolio.snapshot())
                if not decision.approved:
                    rejected += 1
                    with self._lock:
                        self._risk_rejected += 1
                    continue
                result = self.paper_trading.submit_decision(decision)
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
        return CycleResult(cycle, timestamp, candidates, tuple(approved), rejected, tuple(errors))

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._errors.append(message)
