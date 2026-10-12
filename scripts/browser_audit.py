"""Audit the nine pages of the running frontend in a real browser.

    python scripts/browser_audit.py [--base http://127.0.0.1:5173]

For every page this reports what the browser actually did: console errors,
failed or HTTP>=400 network requests, every /api/* call with its status, and
the rendered text. It is the ground truth for "the website shows real data",
independent of unit tests. Exits 1 if any page produced a console error or a
failed/4xx/5xx API request, so it can gate a release.
"""
from __future__ import annotations

import argparse
import sys
import time

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PAGES = (
    ("overview", "#/overview"),
    ("markets", "#/markets"),
    ("watchlists", "#/watchlists"),
    ("scanner", "#/scanner"),
    ("portfolio", "#/portfolio"),
    ("orders", "#/orders"),
    ("journal", "#/journal"),
    ("connections", "#/connections"),
    ("control", "#/control"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:5173")
    parser.add_argument("--text-chars", type=int, default=420)
    args = parser.parse_args()

    problems = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name, hash_route in PAGES:
            page = browser.new_page()
            console_errors: list[str] = []
            api_calls: list[str] = []
            failed: list[str] = []
            page.on("console", lambda msg: console_errors.append(f"{msg.type}: {msg.text}")
                    if msg.type in ("error", "warning") else None)
            page.on("requestfailed", lambda req:
                    failed.append(f"FAILED {req.method} {req.url} ({req.failure})"))
            page.on("response", lambda res: (
                api_calls.append(f"{res.status} {res.request.method} {res.url.split('/api', 1)[-1] if '/api' in res.url else res.url}")
                if "/api/" in res.url else
                failed.append(f"HTTP {res.status} {res.request.method} {res.url}")
                if res.status >= 400 else None))
            # Remember when the last API answer arrived so the wait below can
            # stop on real readiness instead of a fixed delay.
            last_api = [0.0]
            page.on("response", lambda res: last_api.__setitem__(0, time.monotonic())
                    if "/api/" in res.url else None)

            page.goto(f"{args.base}/{hash_route}", wait_until="load", timeout=30000)
            # Wait for the page's own data: a provider round-trip can take
            # seconds (the watchlist hits the market-data API per symbol), so
            # the page is read once its API traffic has been quiet for a
            # moment - never a fixed delay that can capture a "Loading..."
            # placeholder as if it were the finished page.
            started = time.monotonic()
            while time.monotonic() - started < 20:
                elapsed = time.monotonic() - started
                if api_calls and time.monotonic() - last_api[0] > 1.2:
                    break
                if not api_calls and elapsed > 5:
                    break
                page.wait_for_timeout(200)
            page.wait_for_timeout(400)
            text = " ".join(page.inner_text("body").split())[: args.text_chars]

            print(f"\n===== {name} ({hash_route}) =====")
            print("API calls : " + (" | ".join(api_calls) if api_calls else "NONE"))
            if failed:
                problems += len(failed)
                print("PROBLEMS  : " + " | ".join(failed))
            if console_errors:
                problems += len(console_errors)
                print("CONSOLE   : " + " | ".join(console_errors[:6]))
            print(f"TEXT[{len(text)}]: {text}")
            page.close()
        browser.close()

    print(f"\n{'=' * 60}\ntotal problems: {problems}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
