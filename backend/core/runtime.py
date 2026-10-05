"""Local paper-only service composition for the Alpha Trading Platform."""
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.admin import LocalOwnerAuthenticator, OwnerControlPlane
from backend.ai import AIAnalysisService, MockAIAnalysisProvider
from .connections import ConnectionCenter
from backend.database import SQLiteJournalRepository
from backend.execution import AdapterRegistry, ExecutionRouter, MockTradingPlatformAdapter
from backend.market_data import MarketDataService
from backend.market_data.mock_provider import MockMarketDataProvider
from backend.market_data.session import NEW_YORK, market_session
from backend.orchestrator import OrchestratorConfig, TradingOrchestrator
from backend.paper_trading import PaperTradingEngine
from backend.portfolio import Portfolio
from backend.reporting import DailyReportingService, LocalMockNotifier, LogExportService, PerformanceService
from backend.risk import RiskConfig, RiskEngine
from backend.scanner import MarketScannerService, SymbolUniverse, TemporaryTestAnalyzer
from backend.strategies import StrategyEngine


class ApplicationRuntime:
    """Owns the local service graph; execution is restricted to a mock adapter."""

    def __init__(self, *, database_path: str | Path = "data/alpha_trading.sqlite3",
                 owner_token: str | None = None, clock=lambda: datetime.now(timezone.utc)):
        self.clock = clock
        self.repository = SQLiteJournalRepository(database_path)
        self.market_data_provider = MockMarketDataProvider(demo_history=True)
        self.market_data = MarketDataService(self.market_data_provider)
        self.portfolio = Portfolio(100_000.0)
        self.risk = RiskEngine(RiskConfig())
        self.registry = AdapterRegistry()
        self.registry.register(MockTradingPlatformAdapter(cash=100_000.0, clock=clock), "mock")
        self.execution = ExecutionRouter(self.registry, "mock", clock=clock)
        self.paper = PaperTradingEngine(self.risk, self.portfolio, self.execution, journal=self.repository)
        self.scanner = MarketScannerService(
            self.market_data, TemporaryTestAnalyzer(), clock=lambda: datetime(2024, 1, 2, 15, 29, 0, 1, timezone.utc),
        )
        self.strategy = StrategyEngine()
        ai_enabled = os.getenv("AI_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
        local_ai = os.getenv("AI_PROVIDER", "LOCAL/MOCK").upper() in ("MOCK", "LOCAL/MOCK")
        self.orchestrator = TradingOrchestrator(
            market_data=self.market_data, scanner=self.scanner, strategy=self.strategy,
            risk_engine=self.risk, paper_trading=self.paper, portfolio=self.portfolio,
            config=OrchestratorConfig(SymbolUniverse(("F", "SOFI", "PLTR", "AAPL", "BAC"))),
            clock=clock,
            ai_analysis=AIAnalysisService(MockAIAnalysisProvider()) if ai_enabled and local_ai else None,
            journal=self.repository,
        )
        self.performance = PerformanceService(self.repository)
        self.notifier = LocalMockNotifier()
        self.daily_reports = DailyReportingService(self.repository, self.performance, self.notifier)
        self.exports = LogExportService(self.repository)
        self.owner_controls = None
        if owner_token:
            self.owner_controls = OwnerControlPlane(
                LocalOwnerAuthenticator(owner_token), self.orchestrator, self.repository,
            )
        self.connections = ConnectionCenter(
            market_data=self.market_data, universe=self.orchestrator.config.universe,
            execution=self.execution, performance=self.performance, portfolio=self.portfolio,
            risk=self.risk, notifier=self.notifier,
            owner_controls_enabled=self.owner_controls is not None, clock=clock,
            repository=self.repository,
        )
        self._report_stop = threading.Event()
        self._report_thread = threading.Thread(target=self._schedule_reports, name="alpha-daily-report", daemon=True)
        self._report_thread.start()

    def status(self) -> dict:
        state = self.orchestrator.state
        return {
            "service": "alpha-trading-platform", "status": "ok", "mode": "PAPER",
            "market_data_provider": "mock", "execution_adapter": self.execution.identify(),
            "readiness": "LOCKED",
            "market_session": market_session(self.clock()).state,
            "market_trading_day": market_session(self.clock()).is_trading_day,
            "report_notification": "local-only",
            "owner_controls_enabled": self.owner_controls is not None,
            "orchestrator": state, "portfolio": self.portfolio.snapshot(),
            "risk_kill_switch": self.risk.kill_switch_enabled,
            "universe": self.orchestrator.config.universe.symbols,
        }

    def close(self) -> None:
        self._report_stop.set()
        self.orchestrator.stop()
        self._report_thread.join(timeout=1)
        self.repository.close()

    def _schedule_reports(self) -> None:
        sent: set[object] = set()
        while not self._report_stop.wait(20):
            now = self.clock().astimezone(NEW_YORK)
            session = market_session(now)
            if session.is_trading_day and now >= session.regular_close + timedelta(minutes=5) and session.trading_date not in sent:
                try:
                    self.daily_reports.generate_on_demand(
                        session.trading_date, self.portfolio.snapshot(), system_health="OK",
                        notify=os.getenv("NOTIFICATION_ENABLED", "true").lower() in ("1", "true", "yes", "on"),
                    )
                    sent.add(session.trading_date)
                except Exception as error:
                    logging.getLogger(__name__).exception("Daily report generation failed")
                    try:
                        self.repository.record_error(now, f"daily reporting failed: {error}")
                    except Exception:
                        logging.getLogger(__name__).exception("Unable to persist daily reporting failure")
