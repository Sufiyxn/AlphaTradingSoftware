from datetime import datetime, timezone
from dataclasses import replace
from typing import Sequence

from backend.market_data.models import Quote
from backend.portfolio.models import Position

from .models import AccountSnapshot, ExecutionResult, Order, OrderRequest, OrderStatus
from .registry import AdapterRegistry


class ExecutionRouter:
    """Universal execution facade with capability checks before adapter calls."""

    def __init__(self, registry: AdapterRegistry, adapter_name: str,
                 clock=lambda: datetime(2000, 1, 1, tzinfo=timezone.utc)):
        self.registry = registry
        self.adapter_name = adapter_name.strip().lower()
        self._adapter = registry.get(self.adapter_name)
        self._clock = clock
        self._rejection_id = 0

    def identify(self) -> str:
        return self._adapter.identify()

    def capabilities(self):
        return self._adapter.capabilities()

    def is_authenticated(self) -> bool:
        return self._adapter.is_authenticated()

    def connect(self) -> None:
        self._adapter.connect()

    def disconnect(self) -> None:
        self._adapter.disconnect()

    def get_account(self) -> AccountSnapshot:
        if not self._adapter.capabilities().account_retrieval:
            raise NotImplementedError("account retrieval is not supported")
        return self._adapter.get_account()

    def get_positions(self) -> Sequence[Position]:
        if not self._adapter.capabilities().position_retrieval:
            raise NotImplementedError("position retrieval is not supported")
        return self._adapter.get_positions()

    def get_orders(self) -> Sequence[Order]:
        if not self._adapter.capabilities().order_retrieval:
            raise NotImplementedError("order retrieval is not supported")
        return self._adapter.get_orders()

    def get_order(self, order_id: str) -> Order | None:
        if not self._adapter.capabilities().order_retrieval:
            raise NotImplementedError("order retrieval is not supported")
        return self._adapter.get_order(order_id)

    def get_quote(self, symbol: str) -> Quote:
        if not self._adapter.capabilities().market_data:
            raise NotImplementedError("market data is not supported")
        return self._adapter.get_quote(symbol)

    def validate(self, request: OrderRequest) -> str | None:
        caps = self._adapter.capabilities()
        if not isinstance(request.symbol, str) or not request.symbol.strip():
            return "symbol must be a non-empty string"
        if request.quantity <= 0:
            return "quantity must be positive"
        if request.asset_type not in caps.asset_types:
            return f"unsupported asset type: {request.asset_type}"
        if request.exchange and request.exchange.upper() not in {value.upper() for value in caps.exchanges}:
            return f"unsupported exchange/market: {request.exchange}"
        if request.order_type not in caps.order_types:
            return f"unsupported order type: {request.order_type}"
        if request.direction.value == "SHORT" and not caps.short_selling:
            return "short selling is not supported"
        if (request.fractional or not float(request.quantity).is_integer()) and not caps.fractional_shares:
            return "fractional shares are not supported"
        if request.extended_hours and not caps.extended_hours:
            return "extended-hours trading is not supported"
        return None

    def place_order(self, request: OrderRequest) -> Order:
        request = replace(request, symbol=request.symbol.strip().upper() if isinstance(request.symbol, str) else "")
        if request.exchange:
            request = replace(request, exchange=request.exchange.strip().upper())
        reason = self.validate(request)
        if reason:
            self._rejection_id += 1
            return Order(f"REJECTED-{self._rejection_id:06d}", request, OrderStatus.REJECTED,
                         0, None, self._clock(), rejection_reason=reason)
        return self._adapter.place_order(request)

    def place_order_result(self, request: OrderRequest) -> ExecutionResult:
        order = self.place_order(request)
        return ExecutionResult(order.status is not OrderStatus.REJECTED, order, order.rejection_reason)

    def cancel_order(self, order_id: str) -> Order:
        if not self._adapter.capabilities().order_cancellation:
            raise NotImplementedError("order cancellation is not supported")
        return self._adapter.cancel_order(order_id)

    def replace_order(self, order_id: str, request: OrderRequest) -> Order:
        request = replace(request, symbol=request.symbol.strip().upper() if isinstance(request.symbol, str) else "")
        if request.exchange:
            request = replace(request, exchange=request.exchange.strip().upper())
        caps = self._adapter.capabilities()
        if not caps.order_replacement:
            reason = "order replacement is not supported"
        else:
            reason = self.validate(request)
        if reason:
            self._rejection_id += 1
            return Order(f"REJECTED-{self._rejection_id:06d}", request, OrderStatus.REJECTED,
                         0, None, self._clock(), rejection_reason=reason)
        return self._adapter.replace_order(order_id, request)
