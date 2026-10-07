"""Production service composition for the Alpha Trading Platform.

Every dependency is either real and configured, or explicitly NOT_CONFIGURED.
This module constructs no synthetic market data, no demo history, no fabricated
account, and no test fixture; fixtures are injected by tests through
``market_data_provider`` only.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.admin import LocalOwnerAuthenticator, OwnerControlPlane
from backend.ai import AIAnalysisService, LocalRuleBasedAnalysisProvider
from backend.config import Config, load_config
from backend.connections import ConnectionManager, CredentialHandle, default_execution_providers
from backend.database import SQLiteJournalRepository
from backend.execution import (
    AdapterRegistry, ExecutionMode, ExecutionRouter, ExecutionSafetyGate, LocalPaperAdapter,
)
from backend.market_data import MarketDataBridge, MarketDataService, build_market_data_provider
from backend.market_data.session import NEW_YORK, market_session
from backend.orchestrator import OrchestratorConfig, TradingOrchestrator
from backend.paper_trading import PaperTradingEngine, PositionValuationService, reconcile_journal
from backend.portfolio import Portfolio
from backend.reporting import DailyReportingService, LocalMockNotifier, LogExportService, PerformanceService
from backend.risk import RiskEngine
from backend.scanner import MarketScannerService, StrategyAnalyzer, SymbolUniverse
from backend.strategies import StrategyEngine
from backend.tradingview import TradingViewWebhookService

from .connections import ConnectionCenter


class ApplicationRuntime:
    """Owns the production service graph; nothing synthetic is wired here."""

    def __init__(self, *, database_path: str | Path = "data/alpha_trading.sqlite3",
                 owner_token: str | None = None, clock=None,
                 config: Config | None = None, market_data_provider=None):
        self.config = config if config is not None else load_config()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        trading = self.config.trading

        self.repository = SQLiteJournalRepository(database_path)
        self.market_data_provider = (
            market_data_provider if market_data_provider is not None
            else build_market_data_provider(self.config.market_data, clock=self.clock))
        self.market_data = MarketDataService(self.market_data_provider)
        self.market_data_bridge = MarketDataBridge(
            self.market_data, clock=self.clock,
            stale_after_seconds=self.config.market_data.stale_after_seconds)

        self.portfolio = Portfolio(trading.paper_starting_cash, clock=self.clock)
        self.risk = RiskEngine(self.config.risk)

        self.registry = AdapterRegistry()
        self.paper_adapter = LocalPaperAdapter(cash=trading.paper_starting_cash, clock=self.clock)
        self.registry.register(self.paper_adapter, self.paper_adapter.identify())
        self.execution = ExecutionRouter(self.registry, self.paper_adapter.identify(), clock=self.clock)
        # Provider-agnostic execution connections: the local paper account is
        # verified in-process; a client-configured external provider starts
        # CONFIGURED and only ever reports CONNECTED after a real verification.
        self.execution_gate = ExecutionSafetyGate(
            kill_switch=lambda: self.risk.kill_switch_enabled,
            live_enabled=lambda: self.config.trading.selection.live_intent,
        )
        self.execution_providers = default_execution_providers()
        self.connection_manager = ConnectionManager(
            registry=self.execution_providers, clock=self.clock,
            gate=self.execution_gate,
            factory_kwargs={"cash": trading.paper_starting_cash},
        )
        self.connection_manager.add(
            connection_id="paper", provider_id="local_paper",
            label="Local paper account", mode=ExecutionMode.PAPER,
            environment="PAPER", adapter=self.paper_adapter)
        broker = self.config.broker
        if broker.provider and broker.provider != "none":
            self.connection_manager.add(
                connection_id="client", provider_id=broker.provider,
                label=broker.describe()["label"],
                mode=(ExecutionMode.LIVE if broker.environment == "LIVE"
                      else ExecutionMode.PAPER),
                environment=broker.environment,
                credentials=CredentialHandle(
                    {"ALPACA_API_KEY": broker.api_key,
                     "ALPACA_API_SECRET": broker.api_secret},
                    source="process environment"))
        # The paper engine shares this ConnectionManager, so its orders travel
        # the same verification the Connection Center reports.
        self.paper = PaperTradingEngine(self.risk, self.portfolio,
                                        journal=self.repository, clock=self.clock,
                                        connection_manager=self.connection_manager,
                                        connection_id="paper")
        self.valuation = PositionValuationService(
            self.market_data_bridge, self.portfolio, self.paper, journal=self.repository)
        self.reconciliation = reconcile_journal(
            portfolio=self.portfolio, engine=self.paper, repository=self.repository,
            starting_cash=trading.paper_starting_cash, clock=self.clock)

        self.strategy = StrategyEngine(config=self.config.strategy)
        self.scanner = MarketScannerService(
            self.market_data,
            StrategyAnalyzer(self.strategy, minimum_reward_risk=self.config.strategy.min_reward_risk),
            lookback=timedelta(days=trading.history_lookback_days),
            minimum_reward_risk=self.config.strategy.min_reward_risk,
            clock=self.clock,
        )
        self.orchestrator = TradingOrchestrator(
            market_data=self.market_data, scanner=self.scanner, strategy=self.strategy,
            risk_engine=self.risk, paper_trading=self.paper, portfolio=self.portfolio,
            config=OrchestratorConfig(
                SymbolUniverse(trading.universe),
                scan_interval=timedelta(seconds=trading.scan_interval_seconds),
                history_lookback=timedelta(days=trading.history_lookback_days),
                max_candidate_price=trading.max_candidate_price,
            ),
            clock=self.clock,
            ai_analysis=(AIAnalysisService(LocalRuleBasedAnalysisProvider())
                         if self.config.ai.active and self.config.ai.provider == "local" else None),
            journal=self.repository,
        )
        self.performance = PerformanceService(self.repository)
        self.notifier = LocalMockNotifier()
        self.daily_reports = DailyReportingService(self.repository, self.performance, self.notifier)
        self.exports = LogExportService(self.repository)
        # TradingView is a signal source only: it authenticates with its own
        # secret, normalizes at the boundary, and enters the same risk and
        # paper workflow as every other signal path.
        self.tradingview = TradingViewWebhookService(
            settings=self.config.tradingview, paper=self.paper, risk=self.risk,
            portfolio=self.portfolio, journal=self.repository, clock=self.clock,
            allowed_symbols=self.orchestrator.config.universe.symbols,
            max_candidate_price=trading.max_candidate_price,
        )
        self.owner_controls = None
        if owner_token:
            self.owner_controls = OwnerControlPlane(
                LocalOwnerAuthenticator(owner_token), self.orchestrator, self.repository,
            )
        self.connections = ConnectionCenter(
            config=self.config, market_data=self.market_data,
            universe=self.orchestrator.config.universe,
            execution=self.execution, performance=self.performance, portfolio=self.portfolio,
            risk=self.risk, notifier=self.notifier,
            owner_controls_enabled=self.owner_controls is not None, clock=self.clock,
            repository=self.repository, market_data_availability=self.valuation.availability,
            tradingview_status=self.tradingview.connection_state,
            tradingview_check=self.tradingview.self_check,
            execution_connections=self.connection_manager,
        )
        self._report_stop = threading.Event()
        self._report_thread = threading.Thread(target=self._schedule_reports, name="alpha-daily-report", daemon=True)
        self._report_thread.start()
        self._market_stop = threading.Event()
        self._market_thread = threading.Thread(target=self._refresh_market_loop,
                                               name="alpha-market-valuation", daemon=True)
        self._market_thread.start()
        # Establish honest availability and marks from the first request onward.
        try:
            self.refresh_market()
        except Exception:
            logging.getLogger(__name__).exception("Initial market-data valuation failed")

    @property
    def mode(self) -> str:
        return self.config.trading.selection.effective.value

    def refresh_market(self, symbols=None):
        """Fetch real quotes, mark open positions, and evaluate automatic exits."""
        if symbols is None:
            symbols = tuple(dict.fromkeys([
                *self.orchestrator.config.universe.symbols,
                *(position.symbol for position in self.portfolio.positions)]))
        return self.valuation.refresh(symbols)

    def status(self) -> dict:
        state = self.orchestrator.state
        market = self.market_data.status
        availability = self.valuation.availability()
        snapshot = self.portfolio.snapshot()
        return {
            "service": "alpha-trading-platform", "status": "ok",
            "mode": self.mode,
            "mode_selection": {
                "configured": self.config.trading.mode.value,
                "effective": self.mode,
                "live_intent": self.config.trading.selection.live_intent,
            },
            "market_data_provider": market["provider"],
            "market_data_state": market["state"],
            "market_data_reason": market["reason"],
            "market_data_availability": availability["state"],
            "market_data_availability_reason": availability["reason"],
            "market_data_last_quote_at": availability["last_quote_at"],
            "market_data_stale_after_seconds": availability["stale_after_seconds"],
            "valuation_status": snapshot.valuation_status,
            "execution_adapter": self.execution.identify(),
            "readiness": self.connections.readiness()["status"],
            "market_session": market_session(self.clock()).state,
            "market_trading_day": market_session(self.clock()).is_trading_day,
            "report_notification": "local-only",
            "owner_controls_enabled": self.owner_controls is not None,
            "orchestrator": state, "portfolio": snapshot,
            "risk_kill_switch": self.risk.kill_switch_enabled,
            "universe": self.orchestrator.config.universe.symbols,
        }

    def close(self) -> None:
        self._report_stop.set()
        self._market_stop.set()
        self.orchestrator.stop()
        self._report_thread.join(timeout=1)
        self._market_thread.join(timeout=1)
        self.repository.close()

    def _refresh_market_loop(self) -> None:
        interval = self.config.trading.scan_interval_seconds
        while not self._market_stop.wait(interval):
            try:
                self.refresh_market()
            except Exception:
                logging.getLogger(__name__).exception("Scheduled market-data valuation failed")

    def _schedule_reports(self) -> None:
        sent: set[object] = set()
        while not self._report_stop.wait(20):
            now = self.clock().astimezone(NEW_YORK)
            session = market_session(now)
            if session.is_trading_day and now >= session.regular_close + timedelta(minutes=5) and session.trading_date not in sent:
                try:
                    self.daily_reports.generate_on_demand(
                        session.trading_date, self.portfolio.snapshot(), system_health="OK",
                        notify=self.config.notifications.enabled,
                    )
                    sent.add(session.trading_date)
                except Exception as error:
                    logging.getLogger(__name__).exception("Daily report generation failed")
                    try:
                        self.repository.record_error(now, f"daily reporting failed: {error}")
                    except Exception:
                        logging.getLogger(__name__).exception("Unable to persist daily reporting failure")
