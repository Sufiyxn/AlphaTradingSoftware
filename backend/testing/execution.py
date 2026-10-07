"""Scripted execution adapters for automated tests only.

Nothing in this package is imported by :class:`backend.core.runtime`; these
doubles exist so connection-lifecycle tests can exercise authentication
failures, connectivity failures, timeouts, throttling, and account failures
without a network, a vendor, or a fabricated production connection state.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from backend.connections.errors import (
    ExecutionAccountError,
    ExecutionAuthenticationError,
    ExecutionConnectionError,
    ExecutionRateLimitError,
    ExecutionTimeoutError,
)
from backend.execution.capabilities import PlatformCapabilities
from backend.execution.models import (
    AccountSnapshot,
    AssetType,
    Balance,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    normalize_order_status,
)
from backend.portfolio.models import Position


def full_paper_capabilities() -> PlatformCapabilities:
    return PlatformCapabilities(
        asset_types=frozenset({AssetType.STOCK, AssetType.ETF}),
        exchanges=frozenset({"US", "NYSE", "NASDAQ"}),
        order_types=frozenset(OrderType),
        short_selling=True, fractional_shares=True, extended_hours=True,
        order_replacement=True, order_cancellation=True,
        position_retrieval=True, account_retrieval=True, order_retrieval=True,
        market_data=False, paper_trading=True, live_trading=False)


class ScriptedAdapter:
    """Protocol-complete double whose behaviour each test scripts explicitly."""

    def __init__(self, *, name: str = "scripted", capabilities: PlatformCapabilities | None = None,
                 account: AccountSnapshot | None = None, connect_error: Exception | None = None,
                 account_error: Exception | None = None, health: dict | None = None,
                 authenticated: bool = False, place_status: str | None = None,
                 cancel_status: str = "canceled",
                 clock=lambda: datetime(2000, 1, 1, tzinfo=timezone.utc)) -> None:
        self.name = name
        self._capabilities = capabilities or full_paper_capabilities()
        self._account = account or AccountSnapshot(
            100_000.0, 100_000.0, "USD", "scripted-account",
            (Balance("USD", 100_000.0, 100_000.0, 100_000.0),))
        self._connect_error = connect_error
        self._account_error = account_error
        self._health = health
        self._authenticated = authenticated
        self._place_status = place_status
        self._cancel_status = cancel_status
        self._clock = clock
        self._orders: dict[str, Order] = {}
        self._next_id = 1

    # -- identity / capability -------------------------------------------------
    def identify(self) -> str:
        return self.name

    def capabilities(self) -> PlatformCapabilities:
        return self._capabilities

    # -- connection ------------------------------------------------------------
    def is_authenticated(self) -> bool:
        return self._authenticated

    def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self._authenticated = True

    def disconnect(self) -> None:
        self._authenticated = False

    def health_check(self) -> dict:
        if self._health is not None:
            return dict(self._health)
        return {"success": True, "status": "PASSED",
                "detail": "the scripted provider answered the health probe"}

    # -- account / positions ---------------------------------------------------
    def get_account(self) -> AccountSnapshot:
        if self._account_error is not None:
            raise self._account_error
        return self._account

    def get_positions(self) -> Sequence[Position]:
        return ()

    # -- orders ----------------------------------------------------------------
    def get_orders(self) -> Sequence[Order]:
        return tuple(self._orders.values())

    def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def reconcile_orders(self, since: datetime | None = None) -> Sequence[Order]:
        orders = self.get_orders()
        if since is None:
            return orders
        return tuple(order for order in orders if order.created_at >= since)

    def place_order(self, request: OrderRequest) -> Order:
        order_id = f"SCRIPTED-{self._next_id:06d}"
        self._next_id += 1
        status = (normalize_order_status(self._place_status)
                  if self._place_status is not None else OrderStatus.SUBMITTED)
        order = Order(order_id, request, status, 0, None, self._clock(),
                      provider_reference=f"ext-{order_id}")
        self._orders[order_id] = order
        return order

    def cancel_order(self, order_id: str) -> Order:
        existing = self._orders[order_id]
        cancelled = Order(existing.order_id, existing.request,
                          normalize_order_status(self._cancel_status),
                          existing.filled_quantity, existing.average_fill_price,
                          existing.created_at, existing.fills,
                          existing.rejection_reason, existing.provider_reference,
                          updated_at=self._clock())
        self._orders[order_id] = cancelled
        return cancelled

    def replace_order(self, order_id: str, request: OrderRequest) -> Order:
        existing = self._orders[order_id]
        replacement = Order(existing.order_id, request, OrderStatus.PENDING, 0, None,
                            existing.created_at, provider_reference=existing.provider_reference)
        self._orders[order_id] = replacement
        return replacement

    # -- market data is deliberately refused ------------------------------------
    def get_quote(self, symbol: str):
        raise NotImplementedError(
            "execution providers are not a market-data source; "
            "connect a market-data provider or TradingView signal source")


def auth_failure(message: str = "the provider rejected the credentials") -> Exception:
    return ExecutionAuthenticationError(message)


def timeout_failure(message: str = "the provider did not answer in time") -> Exception:
    return ExecutionTimeoutError(message)


def connection_failure(message: str = "the provider could not be reached") -> Exception:
    return ExecutionConnectionError(message)


def rate_limit_failure(message: str = "the provider throttled the request") -> Exception:
    return ExecutionRateLimitError(message)


def account_failure(message: str = "the account could not be verified") -> Exception:
    return ExecutionAccountError(message)
