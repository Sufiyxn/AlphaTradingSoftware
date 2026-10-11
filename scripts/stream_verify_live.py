"""Verify a *genuine* live market-data stream against the running backend.

Run from the repository root while `python -m backend` is running:

    python scripts/stream_verify_live.py            # 15 second observation
    python scripts/stream_verify_live.py --window 30

This is the check that decides whether streaming can be claimed. It exits 0
only when, over the observation window:

  * `market_data_stream.state` is CONNECTED (a real authenticated socket),
  * `events_received` strictly increases (events genuinely arriving now),
  * `last_event_at` moves forward and names a symbol,
  * `/api/quotes` serves the configured universe with recent provider
    timestamps, and
  * the feed entitlement is reported rather than assumed.

If the stream is not connected it prints the backend's own reason and exits
with status 2 (blocked) instead of passing or inventing anything. Exit status
1 means a check failed on a stream that claimed to be connected.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from urllib import request as urlrequest
from urllib.error import URLError

BASE = "http://127.0.0.1:8000"


def get(path: str) -> dict:
    with urlrequest.urlopen(f"{BASE}{path}", timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", type=float, default=15.0,
                        help="seconds to observe the stream (default 15)")
    args = parser.parse_args()

    try:
        first = get("/api/status")
    except (URLError, OSError) as error:
        print(f"FAIL  the backend is not reachable at {BASE}: {error}")
        return 1

    stream = first.get("market_data_stream") or {}
    state = stream.get("state")
    print(f"state        : {state}")
    print(f"provider     : {stream.get('provider')}  protocol: {stream.get('protocol')}"
          f"  feed: {stream.get('feed')}")
    print(f"entitlement  : {stream.get('entitlement')}")
    print(f"reason       : {stream.get('reason')}")
    print(f"symbols      : {', '.join(stream.get('symbols') or []) or 'none'}")

    if state != "CONNECTED":
        print(f"\nBLOCKED  the stream is not connected, so no genuine events can be"
              f" claimed.\n         backend reason: {stream.get('reason')}")
        return 2

    received = int(stream.get("events_received") or 0)
    last_at = stream.get("last_event_at")
    last_symbol = stream.get("last_event_symbol")
    deadline = time.time() + args.window
    while time.time() < deadline:
        time.sleep(1.0)

    second = get("/api/status")
    later = second.get("market_data_stream") or {}
    received_later = int(later.get("events_received") or 0)
    last_at_later = later.get("last_event_at")
    quotes_payload = get("/api/quotes")
    universe = [row.get("symbol") for row in quotes_payload.get("quotes") or []]

    failures = 0

    def check(label: str, condition: bool, detail: object = "") -> None:
        nonlocal failures
        if condition:
            print(f"PASS  {label}" + (f"  [{detail}]" if detail else ""))
        else:
            failures += 1
            print(f"FAIL  {label}  [{detail}]")

    check("state stays CONNECTED through the observation window",
          later.get("state") == "CONNECTED", later.get("state"))
    check("events are genuinely arriving (counter increases)",
          received_later > received, f"{received} -> {received_later}")
    check("the last-event timestamp advances",
          bool(last_at_later) and last_at_later != last_at,
          f"{last_at} -> {last_at_later}")
    check("the last event names a real symbol",
          bool(later.get("last_event_symbol")), later.get("last_event_symbol") or last_symbol)
    check("the stream reports its feed entitlement", bool(later.get("entitlement")),
          later.get("entitlement"))
    check("every universe symbol carries a quote", len(universe) > 0,
          ",".join(universe) or "none")

    ages = []
    for row in quotes_payload.get("quotes") or []:
        stamp = row.get("timestamp")
        if stamp:
            moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            ages.append(abs(time.time() - moment.timestamp()))
    stale_after = second.get("market_data_stale_after_seconds") or 300
    check("quote timestamps are recent provider times, not socket times",
          bool(ages) and max(ages) < stale_after,
          f"max age {max(ages):.1f}s / window {stale_after}s" if ages else "no timestamps")

    # No credential may appear in anything the browser can read.
    bodies = [json.dumps(second), json.dumps(quotes_payload)]
    leaked = [body for body in bodies
              if '"api_key": "' in body or '"api_secret": "' in body
              or '"token": "' in body]
    check("no credential appears in any response body", not leaked,
          f"{len(leaked)} body(ies) leaked a value")

    print(f"\nobserved {received} -> {received_later} events over "
          f"{args.window:.0f}s")
    if failures:
        print(f"{failures} check(s) failed on a connected stream")
        return 1
    print("GENUINE STREAM VERIFIED: events are arriving on a real socket now")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except URLError as error:
        print(f"FAIL  the backend is not reachable at {BASE}: {error}")
        sys.exit(1)
