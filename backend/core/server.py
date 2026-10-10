import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import load_config
from .runtime import ApplicationRuntime
from backend.backtesting import BacktestEngine
from backend.market_data.session import market_session


def create_server(runtime: ApplicationRuntime, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload, content_type="application/json; charset=utf-8"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, default=_json_default).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = min(int(self.headers.get("Content-Length", "0")), 16384)
            if length <= 0:
                return {}
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("request body must be a JSON object")
            return value

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:5173")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Owner-Token")
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            try:
                if path == "/api/health":
                    result = {"status": "ok", "service": "alpha-trading-platform", "mode": runtime.mode}
                elif path == "/api/status":
                    result = runtime.status()
                elif path == "/api/opportunities":
                    result = {"opportunities": runtime.scanner.scan(runtime.orchestrator.config.universe)}
                elif path == "/api/quotes":
                    market = runtime.market_data.status
                    result = {"provider": market.get("provider"),
                              "state": market.get("state"),
                              "reason": market.get("reason"),
                              "quotes": runtime.market_data.get_latest(
                                  runtime.orchestrator.config.universe.symbols)}
                elif path == "/api/candles":
                    result = runtime.market_series.get(
                        _one(query, "symbol") or "",
                        timeframe=_one(query, "timeframe"),
                        days=_float_param(query, "days")).payload()
                elif path == "/api/watchlist":
                    result = runtime.watchlist.snapshot().payload()
                elif path == "/api/scanner":
                    result = _scanner_payload(runtime)
                elif path == "/api/decisions":
                    limit = int(_one(query, "limit") or 100)
                    if not 1 <= limit <= 500:
                        raise ValueError("limit must be between 1 and 500")
                    result = runtime.orchestrator.decisions(limit=limit)
                elif path == "/api/positions":
                    result = _positions_payload(runtime)
                elif path == "/api/orders":
                    open_only = str(_one(query, "open") or "").lower() in ("1", "true", "yes")
                    result = runtime.list_orders(open_only=open_only,
                                                 symbol=_one(query, "symbol"))
                elif path == "/api/portfolio":
                    result = runtime.portfolio.snapshot()
                elif path == "/api/trades":
                    result = {"trades": runtime.repository.rows("trades", symbol=_one(query, "symbol"))[:100]}
                elif path == "/api/activity":
                    limit = int(_one(query, "limit") or 50)
                    if not 1 <= limit <= 200:
                        raise ValueError("limit must be between 1 and 200")
                    symbol = _one(query, "symbol")
                    events = [{"record_type": "system", **row} for row in runtime.repository.events(symbol=symbol, limit=limit)]
                    errors = [{"record_type": "error", **row} for row in runtime.repository.errors(symbol=symbol, limit=limit)]
                    orders = [{"record_type": "order", **row} for row in runtime.repository.rows("orders", symbol=symbol)[:limit]]
                    fills = [{**row, "record_type": "fill",
                              "message": f"executed {row.get('quantity')} @ {row.get('price')}"}
                             for row in runtime.repository.rows("fills", symbol=symbol)[:limit]]
                    decisions = [{**row, "record_type": "risk",
                                  "message": "risk decision: "
                                             + ("APPROVED" if row.get("approved") else "REJECTED")}
                                 for row in runtime.repository.rows("risk_decisions", symbol=symbol)[:limit]]
                    result = {"events": sorted(events + errors + orders + fills + decisions,
                                               key=lambda item: str(item.get("timestamp", "")), reverse=True)[:limit]}
                elif path == "/api/performance":
                    result = runtime.performance.calculate(
                        as_of=market_session(runtime.clock()).trading_date, portfolio=runtime.portfolio.snapshot())
                elif path == "/api/reports":
                    report_day = _date_param(query, "date")
                    if report_day is None:
                        result = {"reports": runtime.repository.list_daily_performance(
                            start=_date_param(query, "start"), end=_date_param(query, "end"))}
                    else:
                        saved = runtime.repository.get_daily_performance(report_day)
                        result = saved if saved is not None else runtime.daily_reports.generate_on_demand(
                            report_day, runtime.portfolio.snapshot(), system_health="OK")
                elif path == "/api/logs":
                    start = _date_param(query, "start")
                    end = _date_param(query, "end")
                    kind = _one(query, "kind") or "all"
                    format = _one(query, "format") or "json"
                    exported = runtime.exports.export(kind, format=format, start=start, end=end,
                                                      symbol=_one(query, "symbol"))
                    if format == "csv":
                        return self._send(200, exported.encode(), "text/csv; charset=utf-8")
                    result = json.loads(exported)
                elif path == "/api/configuration":
                    config = runtime.orchestrator.config
                    result = {"mode": runtime.mode, "universe": config.universe.symbols,
                              "scan_interval_seconds": config.scan_interval.total_seconds(),
                              "history_lookback_days": config.history_lookback.total_seconds() / 86400,
                              "max_candidate_price": config.max_candidate_price,
                              "minimum_reward_risk": runtime.risk.config.min_reward_risk,
                              "owner_controls_enabled": runtime.owner_controls is not None}
                elif path == "/api/capabilities":
                    caps = runtime.execution.capabilities()
                    result = {"adapter": runtime.execution.identify(), "paper_trading": caps.paper_trading,
                              "live_trading": False, "authenticated": runtime.execution.is_authenticated(),
                              "capabilities": caps}
                elif path == "/api/connections":
                    result = runtime.connections.status()
                elif path in ("/api/readiness", "/api/connection/readiness"):
                    result = runtime.connections.readiness()
                elif path == "/api/backtest":
                    end = runtime.clock()
                    start = end - runtime.orchestrator.config.history_lookback
                    histories = {symbol: runtime.market_data.get_historical(symbol, start, end)
                                 for symbol in runtime.orchestrator.config.universe.symbols}
                    retrieved = sum(len(bars) for bars in histories.values())
                    if retrieved == 0:
                        result = {
                            "status": "NO_DATA",
                            "reason": ("no historical bars were retrieved from the configured "
                                       "market-data provider; no backtest was run"),
                            "provider": runtime.market_data.status.get("provider"),
                            "provider_state": runtime.market_data.status.get("state"),
                            "metrics": None,
                            "trades": [],
                            "starting_capital": 0.0,
                            "ending_capital": 0.0,
                            "qualified": False,
                            "completed": False,
                            "errors": ("no historical market data was retrieved",),
                        }
                    else:
                        result = {"status": "OK", **asdict(BacktestEngine().run(histories))}
                else:
                    return self._send(404, {"error": "not found"})
                self._send(200, result)
            except Exception as error:
                self._send(400, {"error": str(error)})

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/webhooks/tradingview":
                # Dedicated webhook authentication: this path never requires
                # X-Owner-Token and never falls through to owner controls.
                return self._tradingview_webhook()
            controls = runtime.owner_controls
            if controls is None:
                return self._send(503, {"error": "owner controls are disabled; set OWNER_CONTROL_TOKEN and restart"})
            token = self.headers.get("X-Owner-Token")
            try:
                body = self._body()
                if parsed.path == "/api/connections/test":
                    controls.authorize_owner(token)
                    section = str(body.get("section", ""))
                    result = runtime.connections.test(section)
                    controls.audit_connection_test(token, section, result)
                    return self._send(200 if result["success"] else 409, result)
                if parsed.path == "/api/orders/cancel":
                    controls.authorize_owner(token)
                    order_id = str(body.get("order_id") or body.get("client_order_id") or "")
                    if not order_id:
                        return self._send(400, {"error": "order_id is required"})
                    outcome = runtime.cancel_order(order_id)
                    status = 200 if outcome["success"] else (
                        404 if outcome["outcome"] == "ORDER_NOT_FOUND" else 409)
                    return self._send(status, outcome)
                if parsed.path == "/api/orders/reconcile":
                    controls.authorize_owner(token)
                    return self._send(200, runtime.reconcile_orders())
                if parsed.path == "/api/positions/close":
                    controls.authorize_owner(token)
                    outcome = runtime.close_position(body.get("symbol"),
                                                     body.get("price"),
                                                     body.get("quantity"))
                    status = 200 if outcome.get("closed") else (
                        404 if outcome.get("code") == "POSITION_NOT_FOUND" else 400)
                    return self._send(status, outcome)
                if parsed.path == "/api/positions/risk":
                    controls.authorize_owner(token)
                    outcome = runtime.change_position_risk(
                        body.get("symbol"), body.get("stop_price"),
                        body.get("target_price"))
                    status = 200 if outcome.get("changed") else (
                        404 if outcome.get("code") == "POSITION_NOT_FOUND" else 400)
                    return self._send(status, outcome)
                actions = {
                    "/api/control/start": lambda: controls.start(token),
                    "/api/control/stop": lambda: controls.stop(token),
                    "/api/control/pause": lambda: controls.pause(token),
                    "/api/control/resume": lambda: controls.resume(token),
                    "/api/control/emergency-stop": lambda: controls.emergency_stop(token, str(body.get("reason", "owner emergency stop"))),
                    "/api/control/reset-emergency-stop": lambda: controls.reset_emergency_stop(token, str(body.get("reason", ""))),
                    "/api/control/terminate": lambda: controls.terminate(token),
                    "/api/control/safe-termination": lambda: controls.terminate(token),
                    "/api/control/configuration": lambda: controls.update_runtime_config(token, str(body.get("key", "")), body.get("value")),
                }
                action = actions.get(parsed.path)
                if action is None:
                    return self._send(404, {"error": "not found"})
                action()
                self._send(200, {"status": "ok", "runtime": runtime.status()})
            except PermissionError as error:
                self._send(401, {"error": str(error)})
            except Exception as error:
                self._send(400, {"error": str(error)})

        def _tradingview_webhook(self):
            """POST /api/webhooks/tradingview - authenticated signal ingestion."""
            try:
                raw_length = (self.headers.get("Content-Length") or "0").strip()
                try:
                    length = int(raw_length)
                except ValueError:
                    return self._send(400, {"status": "error", "code": "INVALID_PAYLOAD",
                                            "message": "Content-Length must be an integer"})
                if length < 0:
                    return self._send(400, {"status": "error", "code": "INVALID_PAYLOAD",
                                            "message": "Content-Length must not be negative"})
                limit = runtime.tradingview.max_body_bytes
                if length > limit:
                    return self._send(413, {"status": "error", "code": "PAYLOAD_TOO_LARGE",
                                            "message": f"request body exceeds {limit} bytes"})
                raw = self.rfile.read(length) if length else b""
                response = runtime.tradingview.handle(raw, self.headers)
                return self._send(response.status, response.body)
            except Exception:
                logging.getLogger(__name__).exception("TradingView webhook request failed")
                return self._send(500, {"status": "error", "code": "INTERNAL_ERROR",
                                        "message": "the webhook failed to process the signal"})

        def log_message(self, format, *args):
            return

    return ThreadingHTTPServer((host, port), Handler)


def _positions_payload(runtime: ApplicationRuntime) -> dict:
    """Open positions with an explicit priced/unpriced state and the reason.

    An unpriced position reports ``current_price`` and ``unrealized_pnl`` as
    ``null`` so no consumer can render a fabricated $0 as a real valuation.
    """
    open_by_symbol = {trade.symbol: trade for trade in runtime.paper.trades}
    positions = []
    for position in runtime.portfolio.positions:
        trade = open_by_symbol.get(position.symbol)
        risk = abs(trade.entry_price - trade.stop_price) if trade else None
        sign = 1 if position.direction.value == "LONG" else -1
        marked = position.market_price
        positions.append({
            "symbol": position.symbol, "direction": position.direction,
            "quantity": position.quantity,
            "entry_price": position.average_entry_price,
            "current_price": marked,
            "price_state": position.price_state.value,
            "price_as_of": position.price_as_of,
            "price_age_seconds": ((runtime.clock() - position.price_as_of).total_seconds()
                                  if position.price_as_of is not None else None),
            "price_reason": position.price_reason,
            "stop_price": trade.stop_price if trade else None,
            "target_price": trade.target_price if trade else None,
            "unrealized_pnl": position.unrealized_pnl,
            "r_multiple": ((marked - position.average_entry_price) * sign / risk)
                          if (risk and marked is not None) else None,
            "auto_exit_enabled": position.auto_exit_enabled,
            "status": "OPEN",
        })
    availability = runtime.valuation.availability()
    snapshot = runtime.portfolio.snapshot()
    return {
        "positions": positions,
        "portfolio": snapshot,
        "market_data_availability": availability["state"],
        "market_data_availability_reason": availability["reason"],
        "market_data_last_quote_at": availability["last_quote_at"],
        "market_data_stale_after_seconds": availability["stale_after_seconds"],
        "valuation_status": snapshot.valuation_status,
        "unpriced_position_count": snapshot.unpriced_position_count,
    }


def _one(query, key):
    values = query.get(key, [])
    return values[0] if values else None


def _float_param(query, key):
    value = _one(query, key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number")


def _scanner_payload(runtime: ApplicationRuntime) -> dict:
    """Per-symbol scan outcomes, including the rejections and their reasons.

    The engine's candidates are the ranked ones, but a scanner that only
    shows its winners cannot explain itself; every analyzed symbol is listed
    with its opportunity (valid or not) and any data failure.
    """
    universe = runtime.orchestrator.config.universe
    market = runtime.market_data.status
    detailed = getattr(runtime.scanner, "scan_detailed", None)
    if callable(detailed):
        ranked, symbol_results = detailed(universe)
        rows = [_scanner_row(item.symbol, item.opportunity, item.error)
                for item in symbol_results]
    else:
        ranked, rows = tuple(runtime.scanner.scan(universe)), []
    seen = {row["symbol"] for row in rows}
    for opportunity in ranked:
        if opportunity.symbol not in seen:
            rows.append(_scanner_row(opportunity.symbol, opportunity, None))
            seen.add(opportunity.symbol)
    return {
        "provider": market.get("provider"),
        "provider_state": market.get("state"),
        "market_session": market_session(runtime.clock()).state,
        "eligible_count": len(ranked),
        "symbol_count": len(rows),
        "symbols": rows,
    }


def _scanner_row(symbol: str, opportunity, error: str | None) -> dict:
    return {
        "symbol": symbol,
        "error": error,
        "eligible": bool(opportunity is not None and opportunity.valid
                         and opportunity.direction.value != "NONE"
                         and opportunity.reward_risk_ratio is not None),
        "opportunity": opportunity,
    }


def _date_param(query, key):
    value = _one(query, key)
    return date.fromisoformat(value) if value else None


def _json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def main() -> None:
    config = load_config()
    settings = config.app
    runtime = ApplicationRuntime(database_path=settings.database_path,
                                 owner_token=settings.owner_token, config=config)
    server = create_server(runtime, settings.host, settings.port)
    if runtime.owner_controls is not None:
        runtime.owner_controls._shutdown = server.shutdown
    print(f"Alpha Trading PAPER API listening at http://{settings.host}:{settings.port}/api/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        runtime.close()
