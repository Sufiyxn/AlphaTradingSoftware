from datetime import datetime, timezone
import logging
from dataclasses import replace

from backend.database import JournalRepository
from backend.execution import ExecutionProvider, LocalPaperAdapter, OrderRequest, OrderStatus
from backend.portfolio import Portfolio
from backend.portfolio.models import PriceState
from backend.risk import RiskEngine
from backend.risk.models import RiskDecision, RiskStatus
from backend.strategies.models import Direction, TradeSetup

from .config import PaperTradingConfig
from .models import PaperTradeRecord, SubmissionResult


class PaperTradingEngine:
    """Executes approved risk decisions and manages open paper trades.

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
                 *, clock=None):
        self.risk_engine = risk_engine
        self.portfolio = portfolio
        self.execution = execution or LocalPaperAdapter(cash=portfolio.cash)
        self.config = config or PaperTradingConfig()
        self.journal = journal
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._records: dict[str, PaperTradeRecord] = {}
        self._setups: dict[str, TradeSetup] = {}

    @property
    def trades(self) -> tuple[PaperTradeRecord, ...]:
        return tuple(self._records[key] for key in self._records)

    def submit(self, setup: TradeSetup) -> SubmissionResult:
        decision = self.risk_engine.evaluate(setup, self.portfolio.snapshot())
        self._journal_call("record_risk_decision", decision)
        if not decision.approved:
            return SubmissionResult(decision, None, None)
        return self.submit_decision(decision)

    def submit_decision(self, decision: RiskDecision, *, analysis=None) -> SubmissionResult:
        """Execute an approved risk decision without evaluating or weakening risk rules."""
        if decision.status is not RiskStatus.APPROVED or decision.quantity <= 0:
            return SubmissionResult(decision, None, None)
        if self.risk_engine.kill_switch_enabled:
            stopped = replace(decision, status=RiskStatus.REJECTED, quantity=0,
                              risk_amount=0.0, reasons=decision.reasons + ("kill switch enabled",))
            self._journal_call("record_risk_decision", stopped)
            return SubmissionResult(stopped, None, None)
        setup = decision.setup

        sign = 1 if setup.direction is Direction.LONG else -1
        fill_price = setup.entry_price + sign * self.config.slippage_per_share
        order = self.execution.place_order(OrderRequest(setup.symbol, setup.direction, decision.quantity, fill_price))
        self._journal_call("record_order", order)
        if order.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) or order.filled_quantity <= 0:
            return SubmissionResult(decision, order, None)

        filled = order.filled_quantity
        actual_fill = order.average_fill_price or fill_price
        self.portfolio.open_position(setup.symbol, setup.direction, filled, actual_fill, setup.timestamp)
        record = PaperTradeRecord(setup.symbol, setup.direction, setup.timestamp, actual_fill,
                                  setup.stop_price, setup.target_price, filled,
                                  setup.strategy_name, order.order_id)
        self._records[order.order_id] = record
        self._setups[order.order_id] = setup
        self._journal_call("record_trade_open", record, setup=setup, risk=decision, analysis=analysis)
        self._journal_call("record_position_event", setup.timestamp, setup.symbol, "OPENED",
                           {"order_id": order.order_id, "quantity": filled, "price": actual_fill})
        return SubmissionResult(decision, order, record)

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
