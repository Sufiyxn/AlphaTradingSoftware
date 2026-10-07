from datetime import datetime, timezone
import logging
import re
from dataclasses import replace

from backend.connections import (
    ConnectionManager,
    ExecutionProviderDescriptor,
    default_execution_providers,
)
from backend.database import JournalRepository
from backend.execution import (
    ExecutionMode,
    ExecutionProvider,
    ExecutionSafetyGate,
    LocalPaperAdapter,
)
from backend.pipeline import ExecutionIntent, PipelineCode, TradingPipeline
from backend.portfolio import Portfolio
from backend.portfolio.models import PriceState
from backend.risk import RiskEngine
from backend.risk.models import RiskDecision
from backend.strategies.models import Direction, TradeSetup

from .config import PaperTradingConfig
from .models import PaperTradeRecord, SubmissionResult


class PaperTradingEngine:
    """Executes trade intents through the canonical TradingPipeline.

    The pipeline owns every pre-order decision: connection verification,
    risk evaluation, the execution safety gate, execution mode, capability
    checks, submission through the selected adapter, status normalization, and
    journaling. This engine owns what surrounds it: paper slippage pricing,
    position bookkeeping, and the stop-loss/take-profit lifecycle.

    Automatic stop-loss/take-profit evaluation runs only against a FRESH real
    price - one produced by a connected market-data provider inside the
    configured staleness window. An unpriced or stale position is never exited
    automatically, and no price is ever derived from an entry, a signal, or a
    previous mark.
    """

    def __init__(self, risk_engine: RiskEngine, portfolio: Portfolio,
                 execution: ExecutionProvider | None = None,
                 config: PaperTradingConfig | None = None,
                 journal: JournalRepository | None = None,
                 *, clock=None,
                 connection_manager: ConnectionManager | None = None,
                 connection_id: str = "paper"):
        self.risk_engine = risk_engine
        self.portfolio = portfolio
        self.config = config or PaperTradingConfig()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.connection_id = str(connection_id or "paper")
        self._execution = execution
        self.connections = (connection_manager if connection_manager is not None
                            else self._own_connections(
                                execution or LocalPaperAdapter(cash=portfolio.cash)))
        self.pipeline = TradingPipeline(
            connections=self.connections, risk_engine=risk_engine, portfolio=portfolio,
            journal=journal, clock=self.clock, connection_id=self.connection_id)
        self._journal = journal
        self._records: dict[str, PaperTradeRecord] = {}
        self._setups: dict[str, TradeSetup] = {}

    # -- wiring: journal and execution adapter ------------------------------

    @property
    def journal(self) -> JournalRepository | None:
        return self._journal

    @journal.setter
    def journal(self, value: JournalRepository | None) -> None:
        # The pipeline journals risk and order evidence; keep both in step
        # when a host (the orchestrator) attaches one repository later.
        self._journal = value
        self.pipeline.journal = value

    @property
    def execution(self):
        """The adapter the ConnectionManager routes this engine's orders to."""
        adapter = self.connections.adapter_for(self.connection_id)
        if adapter is not None:
            return adapter
        return self._execution

    @execution.setter
    def execution(self, adapter) -> None:
        # Swapping the adapter behind a registered connection drops the old
        # verification evidence; the pipeline re-verifies before any order.
        self._execution = adapter
        try:
            self.connections.set_adapter(self.connection_id, adapter)
        except KeyError:
            pass  # no connection registered; the pipeline fails closed

    def _own_connections(self, adapter) -> ConnectionManager:
        """A private ConnectionManager around one in-process adapter.

        Standalone embedders and tests wire their own adapter; orders still
        travel a verified ConnectionManager - there is no direct path.
        """
        identify = getattr(adapter, "identify", None)
        provider_id = str(identify() or "").strip().lower() if callable(identify) else ""
        provider_id = re.sub(r"[^a-z0-9_]+", "_", provider_id).strip("_") or "local_paper"
        registry = default_execution_providers()
        if registry.find(provider_id) is None:
            registry.register(ExecutionProviderDescriptor(
                provider_id=provider_id,
                label=f"In-process adapter {provider_id}",
                implemented=True,
                adapter_factory=lambda **_: adapter,
                authentication="NONE",
                supported_modes=(ExecutionMode.PAPER,),
                environments=("PAPER",),
                metadata=(("role", "in-process adapter wired by the paper trading engine"),),
            ))
        manager = ConnectionManager(
            registry=registry, clock=self.clock,
            gate=ExecutionSafetyGate(
                kill_switch=lambda: self.risk_engine.kill_switch_enabled))
        manager.add(connection_id=self.connection_id, provider_id=provider_id,
                    label="In-process execution adapter", mode=ExecutionMode.PAPER,
                    adapter=adapter)
        return manager

    @property
    def trades(self) -> tuple[PaperTradeRecord, ...]:
        """The open trade book, listed in the same symbol order as the
        positions it mirrors so the two views of one book always line up."""
        return tuple(self._records[key] for key in sorted(
            self._records, key=lambda key: (self._records[key].symbol,
                                            self._records[key].entry_timestamp,
                                            key)))

    # -- canonical submission ------------------------------------------------
    def submit(self, setup: TradeSetup, *, source: str = "internal",
               connection_id: str | None = None, idempotency_key: str | None = None,
               signal_id: str | None = None, occurred_at=None, max_age_seconds=None,
               requested_quantity=None, analysis=None) -> SubmissionResult:
        """Evaluate and execute one trade setup through the canonical pipeline."""
        intent = ExecutionIntent(
            setup=setup, source=source, signal_id=signal_id, occurred_at=occurred_at,
            max_age_seconds=max_age_seconds, requested_quantity=requested_quantity,
            connection_id=connection_id, idempotency_key=idempotency_key)
        return self.execute(intent, analysis=analysis)

    def execute(self, intent: ExecutionIntent, *, analysis=None) -> SubmissionResult:
        """Run one ExecutionIntent through the pipeline, then book the fill."""
        # Booking a fill changes the capacity the *next* intent will be sized
        # against, so submission and booking share the pipeline's lock: two
        # intents racing each other can never both spend the same room.
        with self.pipeline.submission_lock:
            prepared = self._prepare(intent)
            result = self.pipeline.execute(prepared)
            return self._finalize(result, analysis=analysis)

    def submit_decision(self, subject, *, analysis=None,
                        source: str = "internal") -> SubmissionResult:
        """The canonical submission entry used by every signal path.

        Accepts an ``ExecutionIntent`` (the pipeline evaluates risk from the
        setup) or an already-approved ``RiskDecision`` (risk is verified and
        never re-evaluated). Either way the request still travels connection
        verification, the safety gate, the execution mode check, and the
        selected adapter inside :class:`TradingPipeline`.
        """
        with self.pipeline.submission_lock:
            if isinstance(subject, RiskDecision):
                result = self.pipeline.execute_decision(
                    subject, limit_price=self._fill_price(subject.setup), source=source)
            else:
                result = self.pipeline.execute(self._prepare(subject))
            return self._finalize(result, analysis=analysis)

    def _prepare(self, intent: ExecutionIntent) -> ExecutionIntent:
        """Attach the paper slippage-adjusted price the adapter will fill at."""
        if not isinstance(intent, ExecutionIntent):
            return intent  # the pipeline rejects it as INVALID_REQUEST
        if intent.limit_price is None:
            intent = replace(intent, limit_price=self._fill_price(intent.setup))
        return intent

    def _fill_price(self, setup) -> float | None:
        if not isinstance(setup, TradeSetup):
            return None
        sign = 1 if setup.direction is Direction.LONG else -1
        return setup.entry_price + sign * self.config.slippage_per_share

    def _finalize(self, result, *, analysis=None) -> SubmissionResult:
        """Book a confirmed fill, or report the pipeline's outcome honestly."""
        risk, order = result.risk, result.order
        if order is None:
            return self._submission(result, risk, None, None)
        order_id = getattr(order, "order_id", None)
        if result.duplicate or order_id in self._records:
            # An idempotent retry never books twice; the first booking stands.
            return self._submission(result, risk, order, self._records.get(order_id))
        if (result.code != PipelineCode.ACCEPTED or risk is None
                or not risk.approved or result.setup is None):
            return self._submission(result, risk, order, None)

        setup = result.setup
        filled = order.filled_quantity
        actual_fill = order.average_fill_price or result.limit_price
        self.portfolio.open_position(setup.symbol, setup.direction, filled,
                                     actual_fill, setup.timestamp)
        record = PaperTradeRecord(setup.symbol, setup.direction, setup.timestamp,
                                  actual_fill, setup.stop_price, setup.target_price,
                                  filled, setup.strategy_name, order.order_id)
        self._records[order.order_id] = record
        self._setups[order.order_id] = setup
        self._journal_call("record_trade_open", record, setup=setup, risk=risk,
                           analysis=analysis)
        self._journal_call("record_position_event", setup.timestamp, setup.symbol,
                           "OPENED",
                           {"order_id": order.order_id, "quantity": filled,
                            "price": actual_fill})
        return self._submission(result, risk, order, record)

    def _submission(self, result, risk, order, trade) -> SubmissionResult:
        return SubmissionResult(risk, order, trade, code=result.code,
                                reason=result.reason,
                                duplicate=bool(result.duplicate),
                                connection_id=result.connection_id,
                                provider_id=result.provider_id)

    def on_bar(self, symbol: str, timestamp: datetime, high: float, low: float) -> tuple[PaperTradeRecord, ...]:
        """Close touched positions from a real bar; stop wins if both levels hit."""
        symbol = symbol.strip().upper()
        if not self._auto_exit_allowed(symbol):
            return ()
        closed = []
        for order_id, record in tuple(self._records.items()):
            if record.symbol != symbol:
                continue
            if record.direction is Direction.LONG:
                stop_hit, target_hit = low <= record.stop_price, high >= record.target_price
            else:
                stop_hit, target_hit = high >= record.stop_price, low <= record.target_price
            if not stop_hit and not target_hit:
                continue
            sign = 1 if record.direction is Direction.LONG else -1
            raw_exit = record.stop_price if stop_hit else record.target_price
            exit_price = raw_exit - sign * self.config.slippage_per_share
            reason = "STOP" if stop_hit else "TARGET"
            if self._settle(order_id, record, symbol, exit_price, timestamp, reason):
                closed.append(record)
        return tuple(closed)

    def on_quote(self, symbol: str, timestamp: datetime, price: float) -> tuple[PaperTradeRecord, ...]:
        """Close touched positions from a single fresh real quote.

        Requires the position to already carry a FRESH mark, so no automatic
        exit can ever run off a stale, missing, or invented price.
        """
        symbol = symbol.strip().upper()
        if price <= 0:
            raise ValueError("price must be positive")
        if not self._auto_exit_allowed(symbol):
            return ()
        self.portfolio.update_price(symbol, price, timestamp=timestamp, state=PriceState.FRESH)
        closed = []
        for order_id, record in tuple(self._records.items()):
            if record.symbol != symbol:
                continue
            if record.direction is Direction.LONG:
                stop_hit, target_hit = price <= record.stop_price, price >= record.target_price
            else:
                stop_hit, target_hit = price >= record.stop_price, price <= record.target_price
            if not stop_hit and not target_hit:
                continue
            sign = 1 if record.direction is Direction.LONG else -1
            exit_price = price - sign * self.config.slippage_per_share
            reason = "STOP" if stop_hit else "TARGET"
            if self._settle(order_id, record, symbol, exit_price, timestamp, reason):
                closed.append(record)
        return tuple(closed)

    def close(self, symbol: str, price: float, timestamp: datetime | None = None) -> float:
        """Owner-authorized manual close. A real exit price is always required."""
        position = next((item for item in self.portfolio.positions
                         if item.symbol == symbol.strip().upper()), None)
        if position is None:
            raise KeyError(symbol)
        if price is None:
            raise ValueError("a manual close requires an explicit real exit price")
        sign = 1 if position.direction is Direction.LONG else -1
        fill = price - sign * self.config.slippage_per_share
        fees = position.quantity * self.config.commission_per_share
        closed_at = timestamp or self.clock()
        pnl = self.portfolio.close_position(symbol, price=fill, fees=fees, timestamp=closed_at)
        for order_id, record in tuple(self._records.items()):
            if record.symbol == symbol.strip().upper():
                trade_pnl = (fill - record.entry_price) * sign * record.quantity - record.quantity * self.config.commission_per_share
                self._journal_call("record_trade_close", order_id, closed_at, fill, trade_pnl,
                                   context={"reason": "MANUAL", "portfolio_realized_pnl": pnl})
                self._journal_call("record_position_event", closed_at, record.symbol, "CLOSED",
                                   {"order_id": order_id, "quantity": record.quantity, "price": fill,
                                    "pnl": trade_pnl, "reason": "MANUAL"})
                del self._records[order_id]
                self._setups.pop(order_id, None)
        return pnl

    def restore_trade(self, record: PaperTradeRecord, setup: TradeSetup | None = None) -> None:
        """Re-arm a persisted open trade after a restart."""
        self._records[record.order_id] = record
        if setup is not None:
            self._setups[record.order_id] = setup

    def _auto_exit_allowed(self, symbol: str) -> bool:
        position = next((item for item in self.portfolio.positions if item.symbol == symbol), None)
        if position is None:
            return False
        if position.price_state is not PriceState.FRESH:
            return False
        if position.price_as_of is None:
            return False
        return True

    def _settle(self, order_id: str, record: PaperTradeRecord, symbol: str,
                exit_price: float, timestamp: datetime, reason: str) -> bool:
        fee = record.quantity * self.config.commission_per_share
        self.portfolio.update_price(symbol, exit_price, timestamp=timestamp, state=PriceState.FRESH)
        portfolio_pnl = self.portfolio.close_position(
            symbol, record.quantity, exit_price, fees=fee, timestamp=timestamp)
        sign = 1 if record.direction is Direction.LONG else -1
        trade_pnl = (exit_price - record.entry_price) * sign * record.quantity - fee
        self._journal_call("record_trade_close", order_id, timestamp, exit_price, trade_pnl,
                           context={"reason": reason, "portfolio_realized_pnl": portfolio_pnl})
        self._journal_call("record_position_event", timestamp, symbol, "CLOSED",
                           {"order_id": order_id, "quantity": record.quantity, "price": exit_price,
                            "pnl": trade_pnl, "reason": reason})
        self._records.pop(order_id)
        self._setups.pop(order_id, None)
        return True

    def _journal_call(self, method: str, *args, **kwargs) -> None:
        if self.journal is None:
            return
        try:
            getattr(self.journal, method)(*args, **kwargs)
        except Exception:
            logging.getLogger(__name__).exception("Unable to persist paper-trading journal entry using %s", method)
