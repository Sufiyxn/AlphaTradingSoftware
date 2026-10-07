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
        restored += 1

    cash = float(starting_cash)
    realized = 0.0
    daily = 0.0
    closed_ok = 0

    for direction, quantity, entry_price in restored_entries:
        cash -= _sign(direction) * quantity * entry_price

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

    portfolio.restore_ledger(cash=cash, realized_pnl=realized, daily_pnl=daily,
                             trade_count=closed_ok, day=now.date())

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
    return summary


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
