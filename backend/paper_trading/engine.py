from datetime import datetime, timezone
import logging
import math
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
    OrderStatus,
)
from backend.orders import Fill, Order, is_terminal
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
        # Phase 10 checkpoint 3: which durable fills are already inside the
        # position, per order. Booking is keyed by the fill's own identity, so
        # a replayed provider answer can never book the same execution twice.
        self._booked_fills: dict[str, set[str]] = {}
        self._risks: dict[str, RiskDecision] = {}
        self._analyses: dict[str, object] = {}
        self.pipeline.fill_observer = self._on_durable_fills

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
        setup = result.setup
        if (risk is not None and risk.approved and isinstance(setup, TradeSetup)
                and order_id is not None):
            # Remember what the trade is even while the provider has not
            # filled it: a fill that arrives later (checkpoint 3) must still
            # open the position with this setup's stop and target.
            self._setups[order_id] = setup
            self._risks[order_id] = risk
            self._analyses[order_id] = analysis
        self._prune_pending_setups()
        if result.duplicate or order_id in self._records:
            # An idempotent retry never books twice; the first booking stands.
            return self._submission(result, risk, order, self._records.get(order_id))
        if (result.code != PipelineCode.ACCEPTED or risk is None
                or not risk.approved or setup is None):
            return self._submission(result, risk, order, None)

        filled = order.filled_quantity
        actual_fill = order.average_fill_price or result.limit_price
        record = PaperTradeRecord(setup.symbol, setup.direction, setup.timestamp,
                                  actual_fill, setup.stop_price, setup.target_price,
                                  filled, setup.strategy_name, order.order_id)
        self._records[order.order_id] = record
        # Nothing about this order is inside the position yet, so every fill
        # it carries is bookable exactly once.
        self._booked_fills[order.order_id] = set()
        self._book_fills(order, order.fills, opened_at=setup.timestamp,
                         symbol=setup.symbol, direction=setup.direction)
        self._journal_call("record_trade_open", record, setup=setup, risk=risk,
                           analysis=analysis)
        self._journal_call("record_position_event", setup.timestamp, setup.symbol,
                           "OPENED",
                           {"order_id": order.order_id, "quantity": filled,
                            "price": actual_fill})
        return self._submission(result, risk, order, record)

    # -- durable fills -> position (Phase 10 checkpoint 3) -------------------
    def _on_durable_fills(self, order: Order, fills: tuple[Fill, ...]) -> None:
        """Book fills that just became durable, at most once each.

        Called by the order lifecycle the moment a fill is written to the fill
        journal for the first time. Two cases reach here:

        * the trade is already open - a later execution for the same order
          (a partial that completes, a provider's delayed answer) adds to the
          position and to the trade record, so exits still close what was
          actually bought;
        * the trade does not exist yet - an order the pipeline reported as
          accepted-but-unfilled that fills afterwards opens its position here,
          with the setup the submission was approved on.

        A fill that is not new to the order or not new to the journal never
        reaches this method, so a replay books nothing.
        """
        if not fills:
            return
        order_id = order.order_id
        record = self._records.get(order_id)
        if record is None:
            record = self._open_trade_from(order)
            if record is None:
                return
            self._book_fills(order, order.fills, opened_at=record.entry_timestamp,
                             symbol=record.symbol, direction=record.direction)
            self._journal_call("record_trade_open", record,
                               setup=self._setups.get(order_id),
                               risk=self._risks.get(order_id),
                               analysis=self._analyses.get(order_id))
            self._journal_call("record_position_event", record.entry_timestamp,
                               record.symbol, "OPENED",
                               {"order_id": order_id,
                                "quantity": order.filled_quantity,
                                "price": record.entry_price,
                                "source": "durable fill"})
            return
        booked = self._booked_fills.get(order_id)
        if booked is None:
            # Restored after a restart: everything the order already carried
            # was inside the restored position, except the fills being
            # announced right now.
            announced = {fill.fill_id for fill in fills}
            booked = {fill.fill_id for fill in order.fills
                      if fill.fill_id and fill.fill_id not in announced}
            self._booked_fills[order_id] = booked
        fresh = tuple(fill for fill in fills if fill.fill_id not in booked)
        if not fresh:
            return
        added = self._book_fills(order, fresh, symbol=record.symbol,
                                 direction=record.direction)
        if added <= 0:
            return
        added_cost = sum(fill.quantity * fill.price for fill in fresh
                         if fill.quantity > 0 and fill.price > 0)
        quantity = record.quantity + added
        # The trade keeps the average the position now carries: its quantity
        # and entry price are what a restart rebuilds cash from, so leaving
        # the original entry price behind would drift the ledger on restart.
        entry_price = (record.entry_price * record.quantity + added_cost) / quantity
        updated = replace(record, quantity=quantity, entry_price=entry_price)
        self._records[order_id] = updated
        # The trade row must carry the quantity the position now holds, or a
        # restart would rebuild a smaller position than the fills describe.
        setup = self._setups.get(order_id)
        self._journal_call("record_trade_open", updated, setup=setup,
                           risk=self._risks.get(order_id),
                           analysis=self._analyses.get(order_id))
        self._journal_call("record_position_event", fresh[-1].timestamp,
                           updated.symbol, "ADDED",
                           {"order_id": order_id, "quantity": added,
                            "price": (sum(f.quantity * f.price for f in fresh)
                                      / added),
                            "filled_quantity": updated.quantity,
                            "source": "durable fill"})

    def _open_trade_from(self, order: Order) -> PaperTradeRecord | None:
        """The trade record an unrecorded filled order deserves, or ``None``.

        Only a risk-approved submission with a known setup may open a trade,
        and only an order the provider actually filled: a rejected or merely
        submitted order never becomes a position.
        """
        setup = self._setups.get(order.order_id)
        if setup is None:
            return None
        if order.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            return None
        if order.filled_quantity <= 0:
            return None
        record = PaperTradeRecord(
            setup.symbol, setup.direction, setup.timestamp,
            order.average_fill_price or setup.entry_price,
            setup.stop_price, setup.target_price, order.filled_quantity,
            setup.strategy_name, order.order_id)
        self._records[order.order_id] = record
        self._booked_fills[order.order_id] = set()
        return record

    def _book_fills(self, order: Order, fills, *, opened_at=None,
                    symbol: str | None = None,
                    direction: Direction | None = None) -> float:
        """Move these executions into the portfolio; return what was booked.

        Each fill is booked under its own identity, at the price the provider
        reported for it - never at an averaged or invented price - and the
        portfolio keeps the position's original open time.
        """
        order_id = order.order_id
        symbol = symbol or order.symbol
        direction = direction if direction is not None else order.direction
        booked = 0.0
        for fill in fills:
            if fill.fill_id and fill.fill_id in self._booked_fills.setdefault(
                    order_id, set()):
                continue
            if fill.quantity <= 0 or fill.price <= 0:
                continue
            self.portfolio.open_position(symbol, direction,
                                         fill.quantity, fill.price,
                                         opened_at or fill.timestamp)
            booked += float(fill.quantity)
            if fill.fill_id:
                self._booked_fills[order_id].add(fill.fill_id)
        return booked

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

    def close(self, symbol: str, price: float, timestamp: datetime | None = None,
              quantity: int | None = None) -> float:
        """Owner-authorized manual close. A real exit price is always required.

        ``quantity`` scales the exit down to a partial close; omitted it closes
        the whole position, which is what every existing caller asks for. A
        quantity the position does not hold is refused before any state moves.
        """
        symbol = symbol.strip().upper()
        position = next((item for item in self.portfolio.positions
                         if item.symbol == symbol), None)
        if position is None:
            raise KeyError(symbol)
        if price is None:
            raise ValueError("a manual close requires an explicit real exit price")
        q = position.quantity if quantity is None else quantity
        if (isinstance(q, bool) or not isinstance(q, (int, float))
                or not math.isfinite(q) or q != int(q)
                or q <= 0 or q > position.quantity):
            raise ValueError("invalid close quantity")
        q = int(q)
        sign = 1 if position.direction is Direction.LONG else -1
        fill = price - sign * self.config.slippage_per_share
        fees = q * self.config.commission_per_share
        closed_at = timestamp or self.clock()
        pnl = self.portfolio.close_position(symbol, quantity=q, price=fill,
                                            fees=fees, timestamp=closed_at)
        remaining = position.quantity - q
        partial = remaining > 0
        for order_id, record in tuple(self._records.items()):
            if record.symbol != symbol:
                continue
            trade_pnl = ((fill - record.entry_price) * sign * q
                         - q * self.config.commission_per_share)
            if partial:
                self._journal_call(
                    "record_trade_partial", order_id, closed_at, fill, q, trade_pnl,
                    context={"reason": "MANUAL", "portfolio_realized_pnl": pnl,
                             "remaining": remaining})
                self._journal_call(
                    "record_position_event", closed_at, symbol, "PARTIALLY_CLOSED",
                    {"order_id": order_id, "quantity": q, "price": fill,
                     "pnl": trade_pnl, "remaining": remaining, "reason": "MANUAL"})
                # The trade keeps the quantity the position still holds, so a
                # restart restores exactly what is left, never the original.
                self._records[order_id] = replace(record, quantity=record.quantity - q)
                continue
            self._journal_call("record_trade_close", order_id, closed_at, fill, trade_pnl,
                               context={"reason": "MANUAL", "portfolio_realized_pnl": pnl})
            self._journal_call("record_position_event", closed_at, symbol, "CLOSED",
                               {"order_id": order_id, "quantity": record.quantity,
                                "price": fill, "pnl": trade_pnl, "reason": "MANUAL"})
            self._forget(order_id)
        return pnl

    def _forget(self, order_id: str) -> None:
        """Drop every per-order booking memory once the trade is closed."""
        self._records.pop(order_id, None)
        self._setups.pop(order_id, None)
        self._risks.pop(order_id, None)
        self._analyses.pop(order_id, None)
        self._booked_fills.pop(order_id, None)

    def _prune_pending_setups(self) -> None:
        """Forget what was remembered for an order that can no longer fill.

        The approved setup is kept so a fill arriving later still opens the
        trade with its stop and target. Once an order reaches a terminal state
        without an execution there is nothing left to open, so remembering it
        would only accumulate.
        """
        for order_id in tuple(self._setups):
            if order_id in self._records:
                continue
            order = self.pipeline.orders.get(order_id)
            if order is None or (is_terminal(order.status) and not order.fills):
                self._forget(order_id)

    def restore_trade(self, record: PaperTradeRecord, setup: TradeSetup | None = None) -> None:
        """Re-arm a persisted open trade after a restart.

        The fills the restored order already carries are marked booked, so the
        first provider answer after a restart cannot book them a second time;
        a fill the order does *not* carry yet still books normally.
        """
        self._records[record.order_id] = record
        if setup is not None:
            self._setups[record.order_id] = setup
        order = self.pipeline.orders.get(record.order_id)
        if order is not None:
            self._booked_fills[record.order_id] = {
                fill.fill_id for fill in order.fills if fill.fill_id}

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
        self._forget(order_id)
        return True

    def _journal_call(self, method: str, *args, **kwargs) -> None:
        if self.journal is None:
            return
        try:
            getattr(self.journal, method)(*args, **kwargs)
        except Exception:
            logging.getLogger(__name__).exception("Unable to persist paper-trading journal entry using %s", method)
