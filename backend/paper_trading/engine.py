from datetime import datetime, timezone

from backend.execution import ExecutionProvider, MockExecutionProvider, OrderRequest, OrderStatus
from backend.portfolio import Portfolio
from backend.risk import RiskEngine
from backend.strategies.models import Direction, TradeSetup

from .config import PaperTradingConfig
from .models import PaperTradeRecord, SubmissionResult


class PaperTradingEngine:
    def __init__(self, risk_engine: RiskEngine, portfolio: Portfolio,
                 execution: ExecutionProvider | None = None,
                 config: PaperTradingConfig | None = None):
        self.risk_engine = risk_engine
        self.portfolio = portfolio
        self.execution = execution or MockExecutionProvider(cash=portfolio.cash)
        self.config = config or PaperTradingConfig()
        self._records: dict[str, PaperTradeRecord] = {}
        self._setups: dict[str, TradeSetup] = {}

    @property
    def trades(self) -> tuple[PaperTradeRecord, ...]:
        return tuple(self._records[key] for key in self._records)

    def submit(self, setup: TradeSetup) -> SubmissionResult:
        decision = self.risk_engine.evaluate(setup, self.portfolio.snapshot())
        if not decision.approved:
            return SubmissionResult(decision, None, None)

        sign = 1 if setup.direction is Direction.LONG else -1
        fill_price = setup.entry_price + sign * self.config.slippage_per_share
        order = self.execution.place_order(OrderRequest(setup.symbol, setup.direction, decision.quantity, fill_price))
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
        return SubmissionResult(decision, order, record)

    def on_bar(self, symbol: str, timestamp: datetime, high: float, low: float) -> tuple[PaperTradeRecord, ...]:
        """Update marks and close touched positions; stop wins if both levels hit."""
        symbol = symbol.strip().upper()
        position = next((item for item in self.portfolio.positions if item.symbol == symbol), None)
        if position is None:
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
            fee = record.quantity * self.config.commission_per_share
            self.portfolio.update_price(symbol, exit_price)
            self.portfolio.close_position(symbol, record.quantity, exit_price, fees=fee, timestamp=timestamp)
            self._records.pop(order_id)
            self._setups.pop(order_id, None)
            closed.append(record)
        return tuple(closed)

    def close(self, symbol: str, price: float, timestamp: datetime | None = None) -> float:
        position = next((item for item in self.portfolio.positions if item.symbol == symbol.strip().upper()), None)
        if position is None:
            raise KeyError(symbol)
        sign = 1 if position.direction is Direction.LONG else -1
        fill = price - sign * self.config.slippage_per_share
        fees = position.quantity * self.config.commission_per_share
        pnl = self.portfolio.close_position(symbol, price=fill, fees=fees, timestamp=timestamp or datetime.now(timezone.utc))
        for order_id, record in tuple(self._records.items()):
            if record.symbol == symbol.strip().upper():
                del self._records[order_id]
                self._setups.pop(order_id, None)
        return pnl
