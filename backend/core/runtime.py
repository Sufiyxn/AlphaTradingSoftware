"""Local paper-only service composition for the Alpha Trading Platform."""
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.admin import LocalOwnerAuthenticator, OwnerControlPlane
from backend.ai import AIAnalysisService, MockAIAnalysisProvider
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
        self.market_data = MarketDataService(MockMarketDataProvider())
        self.portfolio = Portfolio(100_000.0)
        self.risk = RiskEngine(RiskConfig())
        self.registry = AdapterRegistry()
        self.registry.register(MockTradingPlatformAdapter(cash=100_000.0, clock=clock), "mock")
        self.execution = ExecutionRouter(self.registry, "mock", clock=clock)
        self.paper = PaperTradingEngine(self.risk, self.portfolio, self.execution, journal=self.repository)
        self.scanner = MarketScannerService(
            self.market_data, TemporaryTestAnalyzer(), clock=lambda: datetime(2024, 1, 2, 14, 32, 0, 1, timezone.utc),
        )
        self.strategy = StrategyEngine()
        self.orchestrator = TradingOrchestrator(
            market_data=self.market_data, scanner=self.scanner, strategy=self.strategy,
            risk_engine=self.risk, paper_trading=self.paper, portfolio=self.portfolio,
            config=OrchestratorConfig(SymbolUniverse(("AAPL", "MSFT"))),
            clock=clock, ai_analysis=AIAnalysisService(MockAIAnalysisProvider()), journal=self.repository,
        )
        self.performance = PerformanceService(self.repository)
        self.notifier = LocalMockNotifier()
        self.daily_reports = DailyReportingService(self.repository, self.performance, self.notifier)
        self.exports = LogExportService(self.repository)
        self._report_stop = threading.Event()
        self._report_thread = threading.Thread(target=self._schedule_reports, name="alpha-daily-report", daemon=True)
        self._report_thread.start()
        self.owner_controls = None
        if owner_token:
            self.owner_controls = OwnerControlPlane(
                LocalOwnerAuthenticator(owner_token), self.orchestrator, self.repository,
            )

    def status(self) -> dict:
        state = self.orchestrator.state
        return {
            "service": "alpha-trading-platform", "status": "ok", "mode": "PAPER",
            "market_data_provider": "mock", "execution_adapter": self.execution.identify(),
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
                        session.trading_date, self.portfolio.snapshot(), system_health="OK", notify=True,
                    )
                    sent.add(session.trading_date)
                except Exception as error:
                    try:
                        self.repository.record_error(now, f"daily reporting failed: {error}")
                    except Exception:
                        pass
