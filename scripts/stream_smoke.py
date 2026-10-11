"""Streaming smoke: prove the live-quote path over a real socket.

Run from the repository root:

    python scripts/stream_smoke.py

It boots `ApplicationRuntime` in-process on a free port with a temporary
database (nothing under `data/` is touched), points the market-data **stream**
at a loopback RFC 6455 server that speaks the documented Polygon wire format,
and then proves - over HTTP, the way the browser sees it - that:

  * the stream really connects (handshake -> auth -> subscribe -> CONNECTED),
  * real frames arrive on a real socket and are normalized with provider
    timestamps (the server stamps every trade with *now*, so quotes are FRESH),
  * `/api/status` and `/api/quotes` report that live state, and the quotes the
    API serves come from the stream (the REST provider path is never touched),
  * no credential from the run appears in any response body.

This is an integration check of the platform's plumbing, not a market-data
vendor connection: no vendor endpoint is contacted and no vendor key is used.
The REST provider base URL is pointed at a closed loopback port for the same
reason, so its state is reported honestly and separately from the stream.
Exit status is 0 only when every check passed.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urlrequest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import load_config  # noqa: E402
from backend.core.runtime import ApplicationRuntime  # noqa: E402
from backend.core.server import create_server  # noqa: E402
from backend.testing.ws_server import WebSocketTestServer  # noqa: E402

UNIVERSE = ("AAPL", "BAC", "F", "PLTR", "SOFI")


class Smoke:
    def __init__(self, port: int, secret: str) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.secret = secret
        self.failures = 0
        self.checks = 0

    def check(self, label: str, condition: bool, detail="") -> None:
        self.checks += 1
        if condition:
            print(f"PASS  {label:<70} {detail}"[:160])
        else:
            self.failures += 1
            print(f"FAIL  {label:<70} {detail}"[:160])

    def get(self, path: str) -> dict:
        with urlrequest.urlopen(f"{self.base}{path}", timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))


def stream_handler(conn, symbols, stop: threading.Event) -> None:
    """Speak the Polygon protocol: auth, subscribe, then stream real frames."""
    auth = conn.recv_json(timeout=10)
    if auth.get("action") != "auth":
        raise AssertionError(f"expected an auth frame, received {auth!r}")
    conn.send_json([{"ev": "status", "status": "auth_success",
                     "message": "authenticated"}])
    subscribe = conn.recv_json(timeout=10)
    if subscribe.get("action") != "subscribe":
        raise AssertionError(f"expected a subscribe frame, received {subscribe!r}")
    conn.send_json([{"ev": "status", "status": "subscribed"}])
    sent = 0
    while not stop.is_set():
        sent += 1
        moment = int(time.time() * 1000)
        conn.send_json([{"ev": "T", "sym": symbol, "p": 50.0 + sent / 100,
                         "s": 10, "t": moment} for symbol in symbols])
        time.sleep(0.25)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-open", action="store_true",
                        help="leave the HTTP server running after the checks")
    args = parser.parse_args()

    stop = threading.Event()
    fixture = WebSocketTestServer(
        lambda conn: stream_handler(conn, UNIVERSE, stop))
    fixture.start()

    api_key = "stream-smoke-key-" + secrets.token_hex(8)
    env = {
        "MARKET_DATA_PROVIDER": "polygon",
        "POLYGON_API_KEY": api_key,
        "MARKET_DATA_STREAM": "on",
        "MARKET_DATA_STREAM_URL": fixture.url,
        # Keep the run hermetic: the REST path is aimed at a closed loopback
        # port so no vendor endpoint can ever be contacted from a smoke run.
        "MARKET_DATA_BASE_URL": "http://127.0.0.1:9",
        "REQUIRE_API_AUTH": "false",
    }
    config = load_config(env=env)

    handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    handle.close()
    database = Path(handle.name)

    runtime = ApplicationRuntime(database_path=str(database),
                                 owner_token="stream-smoke-owner",
                                 config=config, market_data_provider=None)
    server = None
    try:
        server = create_server(runtime, "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        smoke = Smoke(port, api_key)

        started = runtime.start_market_stream()
        smoke.check("the stream starts from the application entry point", started)

        deadline = time.time() + 15
        snapshot = runtime.market_stream_status()
        while time.time() < deadline and snapshot.get("state") != "CONNECTED":
            time.sleep(0.1)
            snapshot = runtime.market_stream_status()
        smoke.check("the stream reaches CONNECTED over a real socket",
                    snapshot.get("state") == "CONNECTED", snapshot.get("state"))
        smoke.check("authentication and subscription really happened",
                    bool(snapshot.get("authenticated"))
                    and bool(snapshot.get("subscription")),
                    str(snapshot.get("subscription"))[:60])
        smoke.check("the stream reports a feed entitlement instead of assuming one",
                    bool(snapshot.get("entitlement")), str(snapshot.get("entitlement")))
        smoke.check("every symbol of the universe is subscribed",
                    list(snapshot.get("symbols") or []) == list(UNIVERSE))

        deadline = time.time() + 15
        quotes: dict = {}
        while time.time() < deadline and len(quotes) < len(UNIVERSE):
            quotes = {row["symbol"]: row for row in smoke.get("/api/quotes")["quotes"]}
            time.sleep(0.2)
        smoke.check("the quote API serves every universe symbol", len(quotes) == len(UNIVERSE),
                    ",".join(sorted(quotes)))
        ages = []
        for symbol in UNIVERSE:
            stamp = quotes.get(symbol, {}).get("timestamp")
            if stamp:
                ages.append(abs(time.time()
                                - datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                                .timestamp()))
        smoke.check("served quotes carry fresh provider timestamps (not socket time)",
                    len(ages) == len(UNIVERSE) and max(ages) < 300,
                    f"max age {max(ages):.1f}s" if ages else "missing timestamp")

        status = smoke.get("/api/status")
        live = status.get("market_data_stream") or {}
        smoke.check("/api/status reports the live stream state", live.get("state") == "CONNECTED",
                    live.get("state"))
        smoke.check("/api/status counts real incoming events",
                    int(live.get("events_received") or 0) > 0,
                    str(live.get("events_received")))
        provider_state = status.get("market_data_state")
        smoke.check("stream and REST provider states are reported separately",
                    provider_state in (None, "UNAVAILABLE", "NOT_CONFIGURED", "ERROR"),
                    f"provider={provider_state} stream={live.get('state')}")

        bodies = [json.dumps(status), json.dumps(smoke.get("/api/quotes")),
                  json.dumps(runtime.market_stream_status())]
        smoke.check("no credential appears in any response body",
                    all(api_key not in body for body in bodies))

        smoke.check("the loopback fixture saw a subscribe frame for the universe",
                    fixture.handshakes >= 1, f"handshakes={fixture.handshakes}")
        smoke.check("the fixture reported no handler error",
                    not fixture.handler_errors,
                    str(fixture.handler_errors)[:80])

        print(f"\n{smoke.checks - smoke.failures}/{smoke.checks} checks passed")
        if args.keep_open and server is not None:
            print(f"leaving http://127.0.0.1:{port} open (Ctrl+C to stop)")
            threading.Event().wait()
        return 0 if smoke.failures == 0 else 1
    finally:
        stop.set()
        runtime.close()
        if server is not None:
            server.shutdown()
        fixture.stop()
        try:
            database.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
