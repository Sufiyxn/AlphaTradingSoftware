from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from backend.strategies.models import Direction


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    direction: Direction
    quantity: int
    limit_price: float


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    request: OrderRequest
    status: OrderStatus
    filled_quantity: int
    average_fill_price: float | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    cash: float
    equity: float
    currency: str = "USD"
