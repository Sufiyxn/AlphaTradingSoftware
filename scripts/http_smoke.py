"""HTTP smoke: start the application locally and prove it over HTTP.

Run from the repository root:

    python scripts/http_smoke.py

It boots `ApplicationRuntime` in-process on a free port with a temporary
database (nothing under `data/` is touched), then exercises the HTTP surface
the way a demo would: the documented endpoints parsed as JSON, one paper trade
submitted through the canonical pipeline (the same TradingPipeline every signal
path travels), the orders/positions/exports that trade shows up in, the
owner-token guard, proof that the retired TradingView webhook route is gone,
and a scan of every response body for the credentials the run itself
configured.

No network is opened: market data is not attached, so nothing here can reach a
vendor API (binding constraint 5) and no signal vendor exists to call. The
owner token is generated per run - a smoke credential held in memory for the
length of the process, never read from or written to disk, never printed.

Exit status is 0 only when every check passed.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import load_config  # noqa: E402
from backend.core.runtime import ApplicationRuntime  # noqa: E402
from backend.core.server import create_server  # noqa: E402
from backend.pipeline.models import PipelineCode  # noqa: E402
from backend.tests.pipeline_fixture import trade_setup  # noqa: E402

CLOCK = datetime(2024, 1, 2, 15, 30, tzinfo=timezone.utc)
SIGNAL_ID = "smoke-signal-1"
IDEMPOTENCY_KEY = "smoke-intent-1"
RETIRED_ROUTE = "/api/webhooks/tradingview"

GET_PATHS = (
    "/api/status",
    "/api/configuration",
    "/api/capabilities",
    "/api/readiness",
    "/api/connections",
    "/api/positions",
    "/api/portfolio",
    "/api/opportunities",
    "/api/trades",
    "/api/performance",
    "/api/reports",
    "/api/orders",
    "/api/activity?limit=50",
    "/api/logs?kind=all",
    "/api/logs?kind=fills",
)

# Field names no response body may ever echo, whatever the payload was.
SECRET_FIELDS = ("api_key", "api_secret", "owner_control_token",
                 "webhook_secret")


class Smoke:
    def __init__(self, port: int, secrets_in_use: list[str]) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.secrets_in_use = secrets_in_use
        self.results: list[tuple[str, bool, str]] = []
        self.bodies: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((name, bool(ok), detail))
        return bool(ok)

    def get(self, path: str):
        with urlrequest.urlopen(self.base + path, timeout=15) as response:
            raw = response.read().decode("utf-8")
            self.bodies.append(raw)
            return response.status, json.loads(raw)

    def post(self, path: str, body: bytes, headers: dict):
        request = urlrequest.Request(self.base + path, data=body, headers=headers,
                                     method="POST")
        try:
            with urlrequest.urlopen(request, timeout=15) as response:
                raw = response.read().decode("utf-8")
                self.bodies.append(raw)
                return response.status, json.loads(raw)
        except HTTPError as error:
            raw = error.read().decode("utf-8")
            self.bodies.append(raw)
            return error.code, json.loads(raw)

    def scan_for_secrets(self) -> None:
        haystack = "\n".join(self.bodies)
        for secret in self.secrets_in_use:
            if not secret:
                continue
            self.check(f"no response body contains {len(secret)}-char credential",
                       secret not in haystack)
        lowered = haystack.lower()
        for field in SECRET_FIELDS:
            self.check(f"no response echoes the field {field!r}",
                       f'"{field}"' not in lowered)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-open", action="store_true",
                        help="leave the server running until interrupted")
    options = parser.parse_args()

    owner_token = secrets.token_urlsafe(32)
    config = load_config(env={"OWNER_CONTROL_TOKEN": owner_token})
    database = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    database.close()
    Path(database.name).unlink()

    runtime = ApplicationRuntime(database_path=database.name,
                                 owner_token=owner_token,
                                 clock=lambda: CLOCK, config=config,
                                 market_data_provider=None)
    server = create_server(runtime, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    smoke = Smoke(port, [owner_token])
    try:
        documents = {}
        for path in GET_PATHS:
            try:
                status, document = smoke.get(path)
            except Exception as error:  # noqa: BLE001 - reported as a failed check
                smoke.check(f"GET {path} returns JSON", False, str(error))
                continue
            documents[path] = document
            smoke.check(f"GET {path} returns 200 JSON",
                        status == 200 and isinstance(document, (dict, list)))

        status_body = documents.get("/api/status") or {}
        smoke.check("status reports PAPER mode",
                    str(status_body.get("mode", "")).upper().startswith("PAPER"),
                    str(status_body.get("mode")))
        capabilities = documents.get("/api/capabilities") or {}
        smoke.check("live trading is reported unavailable",
                    capabilities.get("live_trading") is False)
        smoke.check("no order exists before the signal",
                    (documents.get("/api/orders") or {}).get("count") == 0)
        smoke.check("no position exists before the signal",
                    len((documents.get("/api/positions") or {}).get("positions", [])) == 0)

        # One paper trade through the canonical pipeline. The setup goes to the
        # in-process paper engine, which runs the same TradingPipeline (risk
        # evaluation, session gate, safety gate, paper adapter) that every
        # other signal path travels - submitted here with no vendor in the
        # loop, because none exists any more.
        setup = trade_setup(timestamp=runtime.clock())
        accepted = runtime.paper.submit(
            setup, source="pipeline", signal_id=SIGNAL_ID,
            idempotency_key=IDEMPOTENCY_KEY, requested_quantity=5)
        smoke.check("the pipeline accepts a sound 2:1 setup",
                    accepted.accepted and accepted.trade is not None,
                    f"{accepted.code} {accepted.reason or ''}".strip())

        _, orders = smoke.get("/api/orders")
        smoke.check("the order is listed over HTTP", orders.get("count") == 1)
        order = (orders.get("orders") or [{}])[0]
        smoke.check("the order is FILLED with its execution attached",
                    order.get("status") == "FILLED"
                    and order.get("filled_quantity") == 5
                    and len(order.get("fills", [])) == 1)
        smoke.check("the order names the pipeline that raised it",
                    order.get("source") == "pipeline"
                    and order.get("signal_id") == SIGNAL_ID)

        _, positions = smoke.get("/api/positions")
        open_positions = positions.get("positions", [])
        smoke.check("the position the fill booked is listed",
                    len(open_positions) == 1
                    and open_positions[0].get("quantity") == 5)

        _, activity = smoke.get("/api/activity?limit=50")
        kinds = {row.get("record_type") for row in activity.get("events", [])}
        smoke.check("the activity feed shows the risk decision", "risk" in kinds)
        smoke.check("the activity feed shows the execution", "fill" in kinds)

        _, fills = smoke.get("/api/logs?kind=fills")
        fill_rows = [row for row in fills if row.get("record_type") == "fills"]
        smoke.check("the durable fill journal is exported",
                    len(fill_rows) == 1)
        smoke.check("the exported fill names its source",
                    (fill_rows[0].get("context_json") or {}).get("source") == "pipeline"
                    if fill_rows else False)

        duplicate = runtime.paper.submit(
            trade_setup(timestamp=runtime.clock()), source="pipeline",
            signal_id=SIGNAL_ID, idempotency_key=IDEMPOTENCY_KEY,
            requested_quantity=5)
        smoke.check("a replayed intent is refused as a duplicate",
                    duplicate.duplicate
                    and duplicate.code == PipelineCode.DUPLICATE_INTENT,
                    f"{duplicate.code}")
        _, after_duplicate = smoke.get("/api/orders")
        smoke.check("the duplicate booked no second order",
                    after_duplicate.get("count") == 1,
                    str(after_duplicate.get("count")))

        # The retired TradingView webhook route must be gone: an unauthenticated
        # POST to it is simply an unknown path, not a signal path with auth.
        retired = smoke.post(RETIRED_ROUTE,
                             json.dumps({"symbol": "AAPL"}).encode("utf-8"),
                             {"Content-Type": "application/json"})
        smoke.check("the retired TradingView webhook route answers 404",
                    retired[0] == 404, str(retired[0]))

        # An unsafe setup: the same entry with a 1:0.5 reward:risk, which the
        # risk engine must refuse before any order exists.
        unsafe = runtime.paper.submit(
            trade_setup(timestamp=runtime.clock(), target=51.0),
            source="pipeline", signal_id="smoke-signal-unsafe",
            idempotency_key="smoke-intent-unsafe", requested_quantity=5)
        smoke.check("an unsafe setup is refused by risk",
                    unsafe.code == PipelineCode.RISK_REJECTED
                    and unsafe.trade is None,
                    f"{unsafe.code}")
        smoke.check("the refusal names the failing risk rule",
                    bool(unsafe.risk is not None and unsafe.risk.reasons))
        _, unchanged = smoke.get("/api/orders")
        smoke.check("the refused signal created no order", unchanged.get("count") == 1,
                    str(unchanged.get("count")))
        _, rejected_feed = smoke.get("/api/activity?limit=200")
        rejected_rows = [row for row in rejected_feed.get("events", [])
                         if row.get("record_type") == "risk" and not row.get("approved")]
        smoke.check("the rejected risk decision is journaled with its reasons",
                    bool(rejected_rows)
                    and bool(rejected_rows[0].get("reasons_json")),
                    str(rejected_rows[0].get("reasons_json"))[:80] if rejected_rows else "none")

        unguarded = smoke.post("/api/orders/cancel",
                               json.dumps({"order_id": "whatever"}).encode("utf-8"),
                               {"Content-Type": "application/json"})
        smoke.check("state-changing endpoints require the owner token",
                    unguarded[0] == 401)
        unguarded_control = smoke.post("/api/control/start", b"{}",
                                       {"Content-Type": "application/json"})
        smoke.check("owner controls require the owner token",
                    unguarded_control[0] == 401)

        reconcile = smoke.post("/api/orders/reconcile", b"{}",
                               {"Content-Type": "application/json",
                                "X-Owner-Token": owner_token})
        smoke.check("read-only reconcile reports MATCH",
                    reconcile[0] == 200 and reconcile[1].get("status") == "MATCH")

        # Phase 10 checkpoint 6: the owner can move the stop and target of the
        # open position - but only in the risk-reducing direction.
        unguarded_risk = smoke.post(
            "/api/positions/risk",
            json.dumps({"symbol": "AAPL", "stop_price": 49.5}).encode("utf-8"),
            {"Content-Type": "application/json"})
        smoke.check("a risk change requires the owner token",
                    unguarded_risk[0] == 401)
        loosened = smoke.post(
            "/api/positions/risk",
            json.dumps({"symbol": "AAPL", "stop_price": 47.0}).encode("utf-8"),
            {"Content-Type": "application/json", "X-Owner-Token": owner_token})
        smoke.check("a loosened stop is refused",
                    loosened[0] == 400
                    and loosened[1].get("code") == "RISK_NOT_REDUCING",
                    f"{loosened[0]} {loosened[1].get('code')}")
        tightened = smoke.post(
            "/api/positions/risk",
            json.dumps({"symbol": "AAPL", "stop_price": 49.5,
                        "target_price": 55.0}).encode("utf-8"),
            {"Content-Type": "application/json", "X-Owner-Token": owner_token})
        smoke.check("a tightened stop and target are accepted",
                    tightened[0] == 200 and tightened[1].get("changed") is True
                    and tightened[1].get("stop_price", {}).get("new") == 49.5
                    and tightened[1].get("execution") == "PAPER",
                    f"{tightened[0]} {tightened[1].get('code')}")
        _, risk_positions = smoke.get("/api/positions")
        moved = risk_positions.get("positions") or []
        smoke.check("the moved levels are what the positions API serves",
                    len(moved) == 1 and moved[0].get("stop_price") == 49.5
                    and moved[0].get("target_price") == 55.0
                    and moved[0].get("quantity") == 5,
                    str([(item.get("stop_price"), item.get("target_price"))
                         for item in moved]))
        unknown_risk = smoke.post(
            "/api/positions/risk",
            json.dumps({"symbol": "MSFT", "stop_price": 49.5}).encode("utf-8"),
            {"Content-Type": "application/json", "X-Owner-Token": owner_token})
        smoke.check("a risk change for an unheld symbol is not found",
                    unknown_risk[0] == 404, str(unknown_risk[0]))

        # Phase 10 checkpoint 4: the owner can close the paper position the
        # signal opened, without a signal source and without a fabricated price.
        unguarded_close = smoke.post(
            "/api/positions/close",
            json.dumps({"symbol": "AAPL", "price": 50.5}).encode("utf-8"),
            {"Content-Type": "application/json"})
        smoke.check("an owner close requires the owner token",
                    unguarded_close[0] == 401)
        priceless = smoke.post(
            "/api/positions/close", json.dumps({"symbol": "AAPL"}).encode("utf-8"),
            {"Content-Type": "application/json", "X-Owner-Token": owner_token})
        smoke.check("a close without a real price is refused", priceless[0] == 400,
                    str(priceless[1].get("code")))
        # Phase 10 checkpoint 5: the same endpoint scales out instead of only
        # ending the position, and a quantity the position does not hold is
        # refused before anything moves.
        over_close = smoke.post(
            "/api/positions/close",
            json.dumps({"symbol": "AAPL", "price": 50.5, "quantity": 6}).encode("utf-8"),
            {"Content-Type": "application/json", "X-Owner-Token": owner_token})
        smoke.check("a close of more than is held is refused",
                    over_close[0] == 400
                    and over_close[1].get("code") == "INVALID_QUANTITY",
                    f"{over_close[0]} {over_close[1].get('code')}")
        partial = smoke.post(
            "/api/positions/close",
            json.dumps({"symbol": "AAPL", "price": 50.5, "quantity": 2}).encode("utf-8"),
            {"Content-Type": "application/json", "X-Owner-Token": owner_token})
        smoke.check("a partial close reduces the position and reports the remainder",
                    partial[0] == 200 and partial[1].get("partial") is True
                    and partial[1].get("quantity") == 2
                    and partial[1].get("remaining") == 3,
                    f"{partial[0]} {partial[1].get('code')}")
        _, reduced = smoke.get("/api/positions")
        remaining = reduced.get("positions") or []
        smoke.check("the remainder stays open at three shares",
                    len(remaining) == 1 and remaining[0].get("quantity") == 3,
                    str(remaining))
        closed = smoke.post(
            "/api/positions/close",
            json.dumps({"symbol": "AAPL", "price": 50.5}).encode("utf-8"),
            {"Content-Type": "application/json", "X-Owner-Token": owner_token})
        smoke.check("the owner close closes what is left of the paper position",
                    closed[0] == 200 and closed[1].get("closed") is True
                    and closed[1].get("execution") == "PAPER"
                    and closed[1].get("partial") is False
                    and closed[1].get("quantity") == 3
                    and isinstance(closed[1].get("realized_pnl"), (int, float)),
                    f"{closed[0]} {closed[1].get('code')}")
        _, emptied = smoke.get("/api/positions")
        smoke.check("no position remains after the close",
                    emptied.get("positions") == [])
        repeat = smoke.post(
            "/api/positions/close",
            json.dumps({"symbol": "AAPL", "price": 50.5}).encode("utf-8"),
            {"Content-Type": "application/json", "X-Owner-Token": owner_token})
        smoke.check("a second close is refused", repeat[0] == 404,
                    str(repeat[0]))

        smoke.scan_for_secrets()

        _, exported = smoke.get("/api/logs?kind=all")
        smoke.check("a full export carries trades, orders, fills and events",
                    {"trades", "orders", "fills", "system_events"} <= {
                        row.get("record_type") for row in exported})
        smoke.check("the export carries no credential field",
                    not any(field in json.dumps(exported).lower()
                            for field in ("api_key", "owner_control_token")))

        if options.keep_open:
            print(f"server left running on {smoke.base} - Ctrl+C to stop")
            while True:
                thread.join(timeout=3600)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        runtime.close()
        Path(database.name).unlink(missing_ok=True)

    width = max(len(name) for name, _, _ in smoke.results)
    failed = 0
    for name, ok, detail in smoke.results:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"{mark}  {name.ljust(width)}  {detail}")
    print(f"\n{len(smoke.results) - failed}/{len(smoke.results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
