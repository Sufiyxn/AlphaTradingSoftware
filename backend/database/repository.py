import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .serialization import from_json, to_json


class SQLiteJournalRepository:
    """Small standard-library SQLite repository for auditable trade history."""

    def __init__(self, path: str | Path = "data/alpha_trading.sqlite3"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def record_event(self, timestamp: datetime, event_type: str, message: str,
                     *, symbol: str | None = None, context: Any = None) -> None:
        self._insert("system_events", (timestamp.isoformat(), event_type, symbol, message, to_json(context or {})))

    def record_error(self, timestamp: datetime, message: str, *, symbol: str | None = None,
                     context: Any = None) -> None:
        self._insert("errors", (timestamp.isoformat(), symbol, message, to_json(context or {})))

    def record_strategy_signal(self, signal: Any) -> None:
        setup = signal.setup
        symbol = signal.symbol.strip().upper()
        self._insert("strategy_signals", (signal.timestamp.isoformat(), symbol, signal.strategy_name,
                    signal.direction.value, to_json(signal)))

    def record_risk_decision(self, decision: Any) -> None:
        setup = decision.setup
        self._insert("risk_decisions", (setup.timestamp.isoformat(), setup.symbol.strip().upper(),
                    int(decision.approved), to_json(decision.reasons), to_json(decision)))

    def record_order(self, order: Any) -> None:
        self._execute("""INSERT INTO orders(order_id,timestamp,symbol,status,context_json)
            VALUES(?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET status=excluded.status,context_json=excluded.context_json""",
            (order.order_id, order.created_at.isoformat(), order.request.symbol.strip().upper(),
             order.status.value, to_json(order)))

    def record_trade_open(self, trade: Any, *, setup: Any = None, risk: Any = None,
                          analysis: Any = None) -> None:
        context = {"setup": setup, "risk": risk, "ai_analysis": analysis}
        self._execute("""INSERT OR REPLACE INTO trades(
            trade_id,order_id,entry_timestamp,exit_timestamp,symbol,direction,strategy,
            entry_price,stop_price,target_price,exit_price,quantity,pnl,reward_risk_ratio,context_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade.order_id, trade.order_id, trade.entry_timestamp.isoformat(), None,
             trade.symbol.strip().upper(), trade.direction.value, trade.strategy_name,
             trade.entry_price, trade.stop_price, trade.target_price, None, trade.quantity,
             None, getattr(setup, "reward_risk_ratio", None), to_json(context)))

    def record_trade_close(self, order_id: str, timestamp: datetime, exit_price: float, pnl: float,
                           *, context: Any = None) -> None:
        current = self._query("SELECT context_json FROM trades WHERE trade_id=?", (order_id,), ("context_json",))
        details = current[0]["context_json"] if current else {}
        details["close"] = context or {}
        self._execute("""UPDATE trades SET exit_timestamp=?,exit_price=?,pnl=?,context_json=? WHERE trade_id=?""",
                      (timestamp.isoformat(), exit_price, pnl, to_json(details), order_id))

    def record_ai_analysis(self, analysis: Any) -> None:
        self._insert("ai_analyses", (analysis.timestamp.isoformat(), analysis.symbol.strip().upper(),
                    analysis.recommendation.value, analysis.score, to_json(analysis)))

    def record_position_event(self, timestamp: datetime, symbol: str, event_type: str,
                              context: Any = None) -> None:
        self._insert("position_events", (timestamp.isoformat(), symbol.strip().upper(), event_type, to_json(context or {})))

    def closed_trades(self, *, start: date | None = None, end: date | None = None,
                      symbol: str | None = None) -> tuple[dict[str, Any], ...]:
        where = ["exit_timestamp IS NOT NULL"]
        values: list[Any] = []
        if start:
            where.append("exit_timestamp >= ?")
            values.append(datetime.combine(start, datetime.min.time()).isoformat())
        if end:
            where.append("exit_timestamp < ?")
            values.append(datetime.combine(date.fromordinal(end.toordinal() + 1), datetime.min.time()).isoformat())
        if symbol:
            where.append("symbol = ?")
            values.append(symbol.strip().upper())
        return self._query("SELECT * FROM trades WHERE " + " AND ".join(where) + " ORDER BY exit_timestamp,trade_id", values,
                           json_columns=("context_json",))

    def open_trades(self) -> tuple[dict[str, Any], ...]:
        return self._query("SELECT * FROM trades WHERE exit_timestamp IS NULL ORDER BY entry_timestamp,trade_id", (),
                           json_columns=("context_json",))

    def events(self, *, start: date | None = None, end: date | None = None,
               symbol: str | None = None, limit: int | None = None) -> tuple[dict[str, Any], ...]:
        return self._filtered_rows("system_events", "timestamp", start, end, symbol, limit, ("context_json",))

    def errors(self, *, start: date | None = None, end: date | None = None,
               symbol: str | None = None, limit: int | None = None) -> tuple[dict[str, Any], ...]:
        return self._filtered_rows("errors", "timestamp", start, end, symbol, limit, ("context_json",))

    def rows(self, kind: str, *, start: date | None = None, end: date | None = None,
             symbol: str | None = None) -> tuple[dict[str, Any], ...]:
        specs = {
            "trades": ("trades", "COALESCE(exit_timestamp,entry_timestamp)", "symbol", ("context_json",)),
            "orders": ("orders", "timestamp", "symbol", ("context_json",)),
            "signals": ("strategy_signals", "timestamp", "symbol", ("context_json",)),
            "risk_decisions": ("risk_decisions", "timestamp", "symbol", ("reasons_json", "context_json")),
            "ai_analyses": ("ai_analyses", "timestamp", "symbol", ("context_json",)),
            "position_events": ("position_events", "timestamp", "symbol", ("context_json",)),
            "system_events": ("system_events", "timestamp", "symbol", ("context_json",)),
            "errors": ("errors", "timestamp", "symbol", ("context_json",)),
        }
        if kind not in specs:
            raise ValueError(f"unsupported journal export kind: {kind}")
        table, timestamp_col, symbol_col, json_columns = specs[kind]
        return self._filtered_rows(table, timestamp_col, start, end, symbol, None, json_columns,
                                   symbol_column=symbol_col)

    def save_daily_performance(self, day: date, performance: Any) -> None:
        self._execute("INSERT OR REPLACE INTO daily_performance(report_date,context_json) VALUES(?,?)",
                      (day.isoformat(), to_json(performance)))

    def get_daily_performance(self, day: date) -> Any:
        rows = self._query("SELECT context_json FROM daily_performance WHERE report_date=?", (day.isoformat(),), ("context_json",))
        return rows[0]["context_json"] if rows else None

    def list_daily_performance(self, *, start: date | None = None,
                               end: date | None = None) -> tuple[dict[str, Any], ...]:
        where: list[str] = []
        values: list[Any] = []
        if start:
            where.append("report_date >= ?")
            values.append(start.isoformat())
        if end:
            where.append("report_date <= ?")
            values.append(end.isoformat())
        query = "SELECT report_date,context_json FROM daily_performance"
        if where:
            query += " WHERE " + " AND ".join(where)
        return self._query(query + " ORDER BY report_date DESC", values, ("context_json",))

    def _filtered_rows(self, table: str, timestamp_col: str, start: date | None, end: date | None,
                       symbol: str | None, limit: int | None, json_columns: tuple[str, ...],
                       *, symbol_column: str = "symbol") -> tuple[dict[str, Any], ...]:
        where: list[str] = []
        values: list[Any] = []
        if start:
            where.append(f"{timestamp_col} >= ?")
            values.append(datetime.combine(start, datetime.min.time()).isoformat())
        if end:
            where.append(f"{timestamp_col} < ?")
            values.append(datetime.combine(date.fromordinal(end.toordinal() + 1), datetime.min.time()).isoformat())
        if symbol:
            where.append(f"{symbol_column} = ?")
            values.append(symbol.strip().upper())
        query = f"SELECT * FROM {table}" + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY {timestamp_col} DESC"
        if limit is not None:
            query += " LIMIT ?"
            values.append(limit)
        return self._query(query, values, json_columns)

    def _insert(self, table: str, values: tuple[Any, ...]) -> None:
        columns = {
            "system_events": "timestamp,event_type,symbol,message,context_json",
            "errors": "timestamp,symbol,message,context_json",
            "strategy_signals": "timestamp,symbol,strategy,direction,context_json",
            "risk_decisions": "timestamp,symbol,approved,reasons_json,context_json",
            "ai_analyses": "timestamp,symbol,recommendation,score,context_json",
            "position_events": "timestamp,symbol,event_type,context_json",
        }
        fields = columns[table]
        marks = ",".join("?" for _ in values)
        self._execute(f"INSERT INTO {table}({fields}) VALUES({marks})", values)

    def _execute(self, query: str, values: tuple[Any, ...]) -> None:
        with self._lock, self._connection:
            self._connection.execute(query, values)

    def _query(self, query: str, values: tuple[Any, ...] | list[Any],
               json_columns: tuple[str, ...] = ()) -> tuple[dict[str, Any], ...]:
        with self._lock:
            result = self._connection.execute(query, values).fetchall()
        rows = []
        for row in result:
            item = dict(row)
            for column in json_columns:
                if column in item:
                    item[column] = from_json(item[column])
            rows.append(item)
        return tuple(rows)

    def _create_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS trades(
            trade_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, entry_timestamp TEXT NOT NULL,
            exit_timestamp TEXT, symbol TEXT NOT NULL, direction TEXT NOT NULL, strategy TEXT NOT NULL,
            entry_price REAL NOT NULL, stop_price REAL NOT NULL, target_price REAL NOT NULL,
            exit_price REAL, quantity REAL NOT NULL, pnl REAL, reward_risk_ratio REAL, context_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders(order_id TEXT PRIMARY KEY,timestamp TEXT NOT NULL,symbol TEXT NOT NULL,status TEXT NOT NULL,context_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS strategy_signals(id INTEGER PRIMARY KEY,timestamp TEXT NOT NULL,symbol TEXT NOT NULL,strategy TEXT NOT NULL,direction TEXT NOT NULL,context_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS risk_decisions(id INTEGER PRIMARY KEY,timestamp TEXT NOT NULL,symbol TEXT NOT NULL,approved INTEGER NOT NULL,reasons_json TEXT NOT NULL,context_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_analyses(id INTEGER PRIMARY KEY,timestamp TEXT NOT NULL,symbol TEXT NOT NULL,recommendation TEXT NOT NULL,score REAL NOT NULL,context_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS position_events(id INTEGER PRIMARY KEY,timestamp TEXT NOT NULL,symbol TEXT NOT NULL,event_type TEXT NOT NULL,context_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS system_events(id INTEGER PRIMARY KEY,timestamp TEXT NOT NULL,event_type TEXT NOT NULL,symbol TEXT,message TEXT NOT NULL,context_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY,timestamp TEXT NOT NULL,symbol TEXT,message TEXT NOT NULL,context_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_performance(report_date TEXT PRIMARY KEY,context_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_trades_exit ON trades(exit_timestamp);
        CREATE INDEX IF NOT EXISTS ix_events_timestamp ON system_events(timestamp);
        CREATE INDEX IF NOT EXISTS ix_errors_timestamp ON errors(timestamp);
        """
        with self._lock, self._connection:
            self._connection.executescript(schema)
