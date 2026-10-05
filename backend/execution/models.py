from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from backend.market_data.models import Quote
from backend.portfolio.models import Position
from backend.strategies.models import Direction


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class AssetType(str, Enum):
    STOCK = "STOCK"
    ETF = "ETF"
    OPTION = "OPTION"
    FUTURE = "FUTURE"
    FOREX = "FOREX"
    CRYPTO = "CRYPTO"
    BOND = "BOND"
    OTHER = "OTHER"


class MarketSession(str, Enum):
    PRE_MARKET = "PRE_MARKET"
    REGULAR = "REGULAR"
    AFTER_HOURS = "AFTER_HOURS"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    asset_type: AssetType = AssetType.STOCK
    exchange: str | None = None
    currency: str = "USD"
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Balance:
    currency: str
    cash: float
    buying_power: float
    equity: float


@dataclass(frozen=True, slots=True)
class OrderRequest:
    # First four fields preserve the original paper-trading constructor.
    symbol: str
    direction: Direction
    quantity: int | float
    limit_price: float | None = None
    order_type: OrderType = OrderType.LIMIT
    asset_type: AssetType = AssetType.STOCK
    exchange: str | None = None
    fractional: bool = False
    extended_hours: bool = False
    stop_price: float | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    quantity: float
    price: float
    timestamp: datetime
    fee: float = 0.0


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    request: OrderRequest
    status: OrderStatus
    filled_quantity: int
    average_fill_price: float | None
    created_at: datetime
    fills: tuple[Fill, ...] = ()
    rejection_reason: str | None = None


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    cash: float
    equity: float
    currency: str = "USD"
    account_id: str = "local"
    balances: tuple[Balance, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    accepted: bool
    order: Order | None
    reason: str | None = None


# These are already normalized platform-independent market and position models.
UniversalQuote = Quote
UniversalPosition = Position
