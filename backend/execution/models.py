from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from backend.market_data.models import Quote
from backend.portfolio.models import Position
from backend.strategies.models import Direction


class OrderStatus(str, Enum):
    """Normalized order state. Provider words are mapped by
    :func:`normalize_order_status`; anything unrecognized becomes ``UNKNOWN``
    instead of an invented status.

    ``NEW`` is the platform's own pre-submission state - an order the platform
    created but has not yet handed to a provider. Providers never report it, so
    :func:`normalize_order_status` never produces it; a provider word that is
    not recognised becomes ``UNKNOWN`` rather than a guess.
    """

    NEW = "NEW"
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


_STATUS_SYNONYMS: dict[str, OrderStatus] = {
    "pending": OrderStatus.PENDING,
    "received": OrderStatus.PENDING,
    "initialized": OrderStatus.PENDING,
    "created": OrderStatus.PENDING,
    "unacknowledged": OrderStatus.PENDING,
    "submitted": OrderStatus.SUBMITTED,
    "sent": OrderStatus.SUBMITTED,
    "queued": OrderStatus.SUBMITTED,
    "transmitted": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.ACCEPTED,
    "acknowledged": OrderStatus.ACCEPTED,
    "new": OrderStatus.ACCEPTED,
    "open": OrderStatus.ACCEPTED,
    "live": OrderStatus.ACCEPTED,
    "active": OrderStatus.ACCEPTED,
    "working": OrderStatus.ACCEPTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "partiallyfilled": OrderStatus.PARTIALLY_FILLED,
    "partial_fill": OrderStatus.PARTIALLY_FILLED,
    "partial": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "complete": OrderStatus.FILLED,
    "completed": OrderStatus.FILLED,
    "executed": OrderStatus.FILLED,
    "rejected": OrderStatus.REJECTED,
    "refused": OrderStatus.REJECTED,
    "denied": OrderStatus.REJECTED,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
    "user_cancelled": OrderStatus.CANCELLED,
    "user_canceled": OrderStatus.CANCELLED,
    "expired": OrderStatus.EXPIRED,
    "expiration": OrderStatus.EXPIRED,
    "failed": OrderStatus.FAILED,
    "failure": OrderStatus.FAILED,
    "error": OrderStatus.FAILED,
}


def normalize_order_status(value: object) -> OrderStatus:
    """Map a provider's status word onto the normalized vocabulary.

    Unknown, empty, or non-text input becomes ``UNKNOWN`` - the caller learns
    that the state is not confirmed rather than receiving a fabricated status.
    """
    if isinstance(value, OrderStatus):
        return value
    text = str(value if value is not None else "").strip().lower()
    text = text.replace("-", "_").replace(" ", "_")
    return _STATUS_SYNONYMS.get(text, OrderStatus.UNKNOWN)


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


class ExecutionMode(str, Enum):
    """Strict paper/live boundary for a connection or an order intent.

    Configuration and credentials never grant ``LIVE``; the execution safety
    gate decides whether LIVE is authorizable at all.
    """

    PAPER = "PAPER"
    LIVE = "LIVE"


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
    time_in_force: str = "DAY"
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    #: The platform's own id for this order, sent to providers that echo it
    #: back so a remote record can be tied to the order we own. Optional:
    #: a provider that ignores it is not a provider that failed.
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        time_in_force = str(self.time_in_force or "").strip().upper()
        if not time_in_force:
            raise ValueError("time_in_force must not be empty")
        object.__setattr__(self, "time_in_force", time_in_force)
        client_order_id = self.client_order_id
        if client_order_id is not None:
            client_order_id = str(client_order_id).strip()
            object.__setattr__(self, "client_order_id", client_order_id or None)
        if not isinstance(self.execution_mode, ExecutionMode):
            try:
                object.__setattr__(self, "execution_mode",
                                   ExecutionMode(str(self.execution_mode).strip().upper()))
            except ValueError:
                raise ValueError(
                    f"unsupported execution mode {self.execution_mode!r}; "
                    f"expected one of {', '.join(item.value for item in ExecutionMode)}"
                ) from None


@dataclass(frozen=True, slots=True)
class Fill:
    """One executed quantity at one price.

    ``fill_id`` is the platform's own identity for the fill and is assigned
    when the canonical order lifecycle records it; ``provider_fill_id`` is the
    execution venue's identifier and stays ``None`` when the provider does not
    expose one. ``order_id``/``provider_order_id`` tie the fill back to the
    order it belongs to. A fill is only ever created from an execution the
    provider actually reported - submitting an order never mints a fill.
    """

    quantity: float
    price: float
    timestamp: datetime
    fee: float = 0.0
    order_id: str | None = None
    fill_id: str | None = None
    provider_fill_id: str | None = None
    provider_order_id: str | None = None
    side: Direction | None = None


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
    # Provider-side identity and update time. ``provider_reference`` stays
    # None when no external provider reference exists - it is never invented.
    provider_reference: str | None = None
    updated_at: datetime | None = None


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
