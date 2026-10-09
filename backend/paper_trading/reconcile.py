"""Restores in-memory trading state from the persisted trade journal.

The paper portfolio is in-memory, but every trade is journaled. On restart this
module rebuilds open positions, realized P&L, trade counts, and cash from the
journal so a restart cannot silently diverge from the recorded trade book.
Restored positions start UNPRICED: only a connected market-data provider can
mark them.
"""
import logging
from datetime import datetime, timezone

from backend.strategies.models import Direction, TradeSetup

from .engine import PaperTradingEngine
from .models import PaperTradeRecord

RESTORED_REASON = "restored from the trade journal; awaiting a real quote"


def reconcile_journal(*, portfolio, engine: PaperTradingEngine, repository,
                      starting_cash: float, clock=None) -> dict:
    """Rebuild portfolio and paper-trade state from persisted history."""
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if repository is None:
        return {"restored": False, "open_positions": 0, "closed_trades": 0,
                "skipped_rows": 0, "realized_pnl": 0.0, "cash": portfolio.cash}

    open_rows = [dict(row) for row in repository.open_trades()]
    closed_rows = [dict(row) for row in repository.closed_trades()]

    # Parse and restore first so cash is only ever derived from positions that
    # actually exist afterwards - a malformed row cannot move money silently.
    restored = 0
    restored_entries = []
    restored_rows = []
    skipped = 0
    for row in open_rows:
        parsed = _open_row(row)
        record = _trade_record(row, parsed[1]) if parsed else None
        if parsed is None or record is None:
            skipped += 1
            continue
        symbol, direction, quantity, entry_price, opened_at = parsed
        try:
            portfolio.restore_position(symbol, direction, quantity, entry_price, opened_at,
                                       reason=RESTORED_REASON)
        except ValueError:
            skipped += 1
            continue
        engine.restore_trade(record, _trade_setup(row))
        restored_entries.append((direction, quantity, entry_price))
        restored_rows.append(row)
        restored += 1

    cash = float(starting_cash)
    realized = 0.0
    daily = 0.0
    closed_ok = 0
    close_operations = 0

    for direction, quantity, entry_price in restored_entries:
        cash -= _sign(direction) * quantity * entry_price

    # A partial close is real money: the position shrinks (already restored
    # above at its remaining quantity) but the trade row is still open, so its
    # realized P&L lives only in the trade's context and must be added here -
    # otherwise a restart would hand back less cash than the book recorded.
    # Every recorded close - partial or final - is also one trade-count
    # operation, matching what the running portfolio counted.
    for row in restored_rows:
        total, same_day = _partial_realized(row, now.date())
        realized += total
        cash += total
        daily += same_day
        close_operations += _partial_count(row)

    for row in closed_rows:
        parsed = _closed_row(row)
        if parsed is None:
            skipped += 1
            continue
        # A closed trade's net effect on cash is exactly its recorded P&L; the
        # entry debit was already applied while the trade was still open.
        pnl = parsed[4]
        realized += pnl
        cash += pnl
        exit_at = _parse_timestamp(row.get("exit_timestamp"))
        if exit_at is not None and exit_at.date() == now.date():
            daily += pnl
        closed_ok += 1
        # Its partial closes were separate close operations too; the final
        # close recorded in `pnl` above is this row's own operation.
        close_operations += 1 + _partial_count(row)

    portfolio.restore_ledger(cash=cash, realized_pnl=realized, daily_pnl=daily,
                             trade_count=close_operations, day=now.date())

    summary = {"restored": restored > 0, "open_positions": restored,
               "closed_trades": closed_ok, "skipped_rows": skipped,
               "realized_pnl": realized, "cash": portfolio.cash}

    if restored or closed_ok:
        try:
            repository.record_event(
                now, "JOURNAL_RECONCILED",
                f"reconciled {restored} open position(s) and {closed_ok} closed trade(s) "
                "from the journal; open positions await a real quote",
                context=summary)
        except Exception:
            logging.getLogger(__name__).exception("Unable to persist reconciliation event")
    if skipped:
        logging.getLogger(__name__).warning(
            "Journal reconciliation skipped %d row(s) that could not be parsed", skipped)
    reported = report_durable_fill_divergence(engine=engine, repository=repository, now=now)
    summary["fill_divergences"] = reported
    return summary


def report_durable_fill_divergence(*, engine: PaperTradingEngine, repository,
                                   now: datetime) -> int:
    """Journal durable fills that no trade row accounts for.

    Positions are rebuilt from the trade book, so a fill the journal holds but
    no trade describes would vanish on a restart. That is a real divergence,
    so it is reported as a discrepancy - it is never silently ignored and
    never turned into a position: the platform does not open a trade it
    cannot reconstruct from its own records. Returns how many were reported.
    """
    if repository is None:
        return 0
    known: set[str] = set()
    for row in (*repository.open_trades(), *repository.closed_trades()):
        for key in ("order_id", "trade_id"):
            value = row.get(key)
            if value:
                known.add(str(value))
    orders = tuple(engine.pipeline.orders.list())
    order_ids = {order.order_id for order in orders}
    reported = 0

    def note(message: str, *, symbol: str | None, context: dict) -> None:
        nonlocal reported
        reported += 1
        try:
            repository.record_event(now, "RECONCILIATION_DISCREPANCY", message,
                                    symbol=symbol, context=context)
        except Exception:  # noqa: BLE001 - a report must not break recovery
            logging.getLogger(__name__).exception(
                "Unable to persist a durable-fill divergence report")

    for order in orders:
        if order.filled_quantity <= 0 or order.order_id in known:
            continue
        note("the journal holds executions for an order no trade row describes",
             symbol=order.symbol,
             context={"kind": "DURABLE_FILL_WITHOUT_TRADE",
                      "order_id": order.order_id,
                      "symbol": order.symbol,
                      "filled_quantity": order.filled_quantity,
                      "status": order.status.value})

    seen: set[str] = set()
    for row in repository.rows("fills"):
        owner = str(row.get("client_order_id") or row.get("order_id") or "").strip()
        if not owner or owner in known or owner in order_ids or owner in seen:
            continue
        seen.add(owner)
        note("the fill journal holds executions for an order the journal "
             "cannot reconstruct",
             symbol=row.get("symbol"),
             context={"kind": "DURABLE_FILL_WITHOUT_ORDER",
                      "order_id": owner,
                      "symbol": row.get("symbol"),
                      "fill_id": row.get("fill_id")})
    return reported


def _sign(direction: Direction) -> int:
    return 1 if direction is Direction.LONG else -1


def _parse_timestamp(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _direction(value) -> Direction | None:
    try:
        direction = Direction(value)
    except (TypeError, ValueError):
        return None
    return direction if direction in (Direction.LONG, Direction.SHORT) else None


def _open_row(row) -> tuple[str, Direction, int, float, datetime] | None:
    direction = _direction(row.get("direction"))
    opened_at = _parse_timestamp(row.get("entry_timestamp"))
    symbol = str(row.get("symbol") or "").strip().upper()
    quantity = row.get("quantity")
    entry_price = row.get("entry_price")
    if direction is None or opened_at is None or not symbol:
        return None
    if not isinstance(quantity, (int, float)) or quantity <= 0:
        return None
    if not isinstance(entry_price, (int, float)) or entry_price <= 0:
        return None
    return symbol, direction, int(quantity), float(entry_price), opened_at


def _closed_row(row) -> tuple[str, Direction, int, float, float] | None:
    opened = _open_row(row)
    if opened is None:
        return None
    symbol, direction, quantity, entry_price, _ = opened
    pnl = row.get("pnl")
    if not isinstance(pnl, (int, float)):
        return None
    return symbol, direction, quantity, entry_price, float(pnl)


def _partial_realized(row, today) -> tuple[float, float]:
    """Realized P&L an open trade booked through partial closes.

    Returns the total and the part landing on ``today`` (for daily P&L), read
    from the partial closes the journal recorded. A trade that was never
    partially closed contributes nothing.
    """
    details = row.get("context_json")
    if not isinstance(details, dict):
        return 0.0, 0.0
    entries = details.get("partials")
    if not isinstance(entries, list):
        return 0.0, 0.0
    total = 0.0
    same_day = 0.0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pnl = entry.get("pnl")
        if not isinstance(pnl, (int, float)) or isinstance(pnl, bool):
            continue
        total += float(pnl)
        at = _parse_timestamp(entry.get("timestamp"))
        if at is not None and at.date() == today:
            same_day += float(pnl)
    return total, same_day


def _partial_count(row) -> int:
    """How many partial closes the journal recorded for this trade."""
    details = row.get("context_json")
    if not isinstance(details, dict):
        return 0
    entries = details.get("partials")
    if not isinstance(entries, list):
        return 0
    return sum(1 for entry in entries if isinstance(entry, dict))


def _trade_record(row, direction: Direction) -> PaperTradeRecord | None:
    entry_timestamp = _parse_timestamp(row.get("entry_timestamp"))
    try:
        return PaperTradeRecord(
            symbol=str(row.get("symbol")).strip().upper(),
            direction=direction,
            entry_timestamp=entry_timestamp,
            entry_price=float(row.get("entry_price")),
            stop_price=float(row.get("stop_price")),
            target_price=float(row.get("target_price")),
            quantity=int(row.get("quantity")),
            strategy_name=str(row.get("strategy") or ""),
            order_id=str(row.get("trade_id") or row.get("order_id") or ""),
        )
    except (TypeError, ValueError):
        return None


def _trade_setup(row) -> TradeSetup | None:
    payload = row.get("context_json") or {}
    setup = payload.get("setup") if isinstance(payload, dict) else None
    if not isinstance(setup, dict):
        return None
    direction = _direction(setup.get("direction"))
    timestamp = _parse_timestamp(setup.get("timestamp"))
    if direction is None or timestamp is None:
        return None
    try:
        return TradeSetup(
            symbol=str(setup.get("symbol")).strip().upper(),
            direction=direction,
            entry_price=float(setup.get("entry_price")),
            stop_price=float(setup.get("stop_price")),
            target_price=float(setup.get("target_price")),
            risk_per_share=float(setup.get("risk_per_share")),
            reward_per_share=float(setup.get("reward_per_share")),
            reward_risk_ratio=float(setup.get("reward_risk_ratio")),
            timestamp=timestamp,
            strategy_name=str(setup.get("strategy_name") or ""),
            score=float(setup.get("score")),
            explanation=str(setup.get("explanation") or ""),
        )
    except (TypeError, ValueError):
        return None
