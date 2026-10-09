"""Production service composition for the Alpha Trading Platform.

Every dependency is either real and configured, or explicitly NOT_CONFIGURED.
This module constructs no synthetic market data, no demo history, no fabricated
account, and no test fixture; fixtures are injected by tests through
``market_data_provider`` only.
"""
import logging
import math
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
from backend.market_data.session import NEW_YORK, AssetClass, market_session, session_gate
from backend.orders.provider import snapshot_from_adapter
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
        self.risk = RiskEngine(self.config.risk, clock=self.clock)

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
            # The provider instance each connection mints order identities
            # under is durable: a restart recovers it instead of reusing it.
            journal=self.repository,
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
                                        connection_id="paper",
                                        session_submission=trading.session_submission)
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

    # ----------------------------------------------------------- order lifecycle
    @property
    def orders(self):
        """The canonical order lifecycle: state, journal, reconciliation.

        This is the same object the pipeline writes through, so what the API
        reports is exactly what was recorded - not a second, drift-prone copy.
        """
        return self.paper.pipeline.orders

    def list_orders(self, *, open_only: bool = False,
                    symbol: str | None = None) -> dict:
        orders = self.orders.list(open_only=open_only, symbol=symbol)
        return {
            "orders": [order.describe() for order in orders],
            "count": len(orders),
            "open_count": len(self.orders.store.open_orders()),
            "pending_reservation_count": len(self.orders.pending_reservations()),
        }

    def cancel_order(self, order_id: str) -> dict:
        """Cancel one order under the submission lock; failures stay visible."""
        with self.paper.pipeline.submission_lock:
            return self.orders.cancel(str(order_id or "")).to_dict()

    def close_position(self, symbol, price, quantity=None) -> dict:
        """Owner-authorized close of one open paper position (phase 10 CP4/CP5).

        The exit price is supplied by the caller and is never defaulted,
        marked or derived from the entry: a fabricated exit price would be a
        fabricated P&L. An optional quantity scales the exit down to a partial
        close - it defaults to the whole position, so the original CP4
        behaviour is unchanged - and a quantity the position does not hold is
        refused before anything moves. The close runs under the submission lock
        so it cannot interleave with a submission or a booking, and it reuses
        the same slippage, fee, journal and trade-close accounting as an
        automatic or signal-driven exit.
        """
        name = str(symbol or "").strip().upper()
        if not name:
            return {"closed": False, "code": "INVALID_SYMBOL",
                    "error": "symbol is required"}
        if (isinstance(price, bool) or not isinstance(price, (int, float))
                or not math.isfinite(price) or price <= 0):
            return {"closed": False, "code": "INVALID_PRICE",
                    "error": "price must be a positive finite number"}
        if quantity is not None:
            if (isinstance(quantity, bool) or not isinstance(quantity, (int, float))
                    or not math.isfinite(quantity) or quantity != int(quantity)
                    or quantity <= 0):
                return {"closed": False, "code": "INVALID_QUANTITY",
                        "error": "quantity must be a positive whole number"}
            quantity = int(quantity)
        with self.paper.pipeline.submission_lock:
            position = next((item for item in self.portfolio.positions
                             if item.symbol == name), None)
            if position is None:
                return {"closed": False, "code": "POSITION_NOT_FOUND",
                        "error": f"no open position exists for {name}"}
            held = position.quantity
            if quantity is not None and quantity > held:
                return {"closed": False, "code": "INVALID_QUANTITY",
                        "error": f"{name} holds {held}; cannot close {quantity}"}
            closing = held if quantity is None else quantity
            direction = position.direction.value
            try:
                realized = self.paper.close(name, float(price),
                                            timestamp=self.clock(),
                                            quantity=closing)
            except (KeyError, ValueError) as error:
                return {"closed": False, "code": "CLOSE_FAILED",
                        "error": str(error)}
        remaining = held - closing
        return {"closed": True, "symbol": name, "quantity": closing,
                "remaining": remaining, "partial": remaining > 0,
                "direction": direction, "requested_price": float(price),
                "realized_pnl": realized, "reason": "MANUAL",
                "execution": "PAPER"}

    def change_position_risk(self, symbol, stop_price=None,
                             target_price=None) -> dict:
        """Owner-authorized stop/target adjustment on one open position.

        Runs under the submission lock like a close, and hands every rule -
        risk-reducing only, stop on the losing side of entry, target beyond
        entry, configured reward:risk floor - to the paper engine, which also
        owns the records the automatic exit reads.
        """
        with self.paper.pipeline.submission_lock:
            return self.paper.adjust_risk(symbol, stop_price=stop_price,
                                          target_price=target_price,
                                          timestamp=self.clock())

    def reconcile_orders(self) -> dict:
        """Compare the journal with the execution provider, read-only."""
        connection_id = self.paper.connection_id
        try:
            adapter = self.connection_manager.adapter_for(connection_id)
        except Exception:  # noqa: BLE001 - an unusable connection is a finding
            adapter = None
        if adapter is None:
            adapter = self.execution
        identify = getattr(adapter, "identify", None)
        provider_id = str(identify() or "") if callable(identify) else None
        snapshot = snapshot_from_adapter(adapter, now=self.clock(),
                                         connection_id=connection_id,
                                         provider_id=provider_id)
        with self.paper.pipeline.submission_lock:
            report = self.orders.reconcile(snapshot, positions=self.portfolio.positions,
                                           now=self.clock())
        return report.to_dict()

    def _market_session_allows_orders(self) -> bool:
        """Whether the session gate would accept a submission right now.

        This is the same decision the pipeline enforces (Phase 11), recomputed
        for display only: the calendar and the configured policy decide, and
        the provider's extended-hours capability is read fail-closed, so the
        status can never claim "allowed" for a provider that would refuse.
        """
        try:
            extended = bool(getattr(self.execution.capabilities(), "extended_hours",
                                    False))
        except Exception:  # noqa: BLE001 - status must not fail on a provider
            extended = False
        return session_gate(
            self.clock(), policy=self.config.trading.session_submission,
            provider_extended_hours=extended,
            asset_class=AssetClass.US_EQUITY).allowed

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
            "market_session_policy": self.config.trading.session_submission,
            "market_session_orders_allowed": self._market_session_allows_orders(),
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
