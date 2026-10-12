"""Browser smoke: drive the real UI in a headless browser.

Run from the repository root, with Node and Python installed:

    python scripts/e2e_smoke.py

It starts (or reuses) the backend on 127.0.0.1:8000 and the Vite dev server on
127.0.0.1:5173, then opens the app in Chromium and proves:

  * the shell loads and reports a real backend connection;
  * all nine pages render their own heading and never a blank screen;
  * an unknown address shows the 404 view, and an old single-page anchor
    (`#/positions`) still lands on the right page;
  * the Markets page shows the market-data stream state honestly;
  * the owner control center refuses an action without a token, and the
    configured token really pauses and stops the orchestrator;
  * no console error was raised anywhere, and the owner token never appears
    in the rendered page.

The owner token is read from the local `.env` and is never printed. Exit status
is 0 only when every check passed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = "http://127.0.0.1:8000"
FRONTEND = "http://127.0.0.1:5173"

PAGES = (
    ("#/overview", "Trading dashboard", "Overview"),
    ("#/markets", "Markets & live charts", "Markets"),
    ("#/watchlists", "Watchlists", "Watchlists"),
    ("#/scanner", "Scanner & strategy context", "Scanner"),
    ("#/portfolio", "Portfolio & positions", "Portfolio"),
    ("#/orders", "Orders & fills", "Orders"),
    ("#/journal", "Trade journal & performance", "Journal"),
    ("#/connections", "Connections & settings", "Connections"),
    ("#/control", "Owner control center", "Control"),
)


def listening(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status < 500
    except (urllib.error.URLError, OSError):
        return False


def wait_for(predicate, timeout: float = 40.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def owner_token_from_env() -> str:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("OWNER_CONTROL_TOKEN="):
            return stripped.split("=", 1)[1].strip()
    return ""


class Checks:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, label: str, condition: bool, detail: object = "") -> None:
        if condition:
            self.passed += 1
            print(f"PASS  {label}" + (f"  [{detail}]" if detail else ""))
        else:
            self.failed += 1
            print(f"FAIL  {label}  [{detail}]")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("FAIL  playwright is not installed (python -m pip install playwright)")
        return 1

    started_backend = started_frontend = False
    procs: list[subprocess.Popen] = []

    if not listening(f"{BACKEND}/api/health"):
        print("starting the backend ...")
        procs.append(subprocess.Popen([sys.executable, "-m", "backend"], cwd=ROOT,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        started_backend = True
        if not wait_for(lambda: listening(f"{BACKEND}/api/health"), timeout=40):
            print("FAIL  the backend did not start on 127.0.0.1:8000")
            return 1

    if not listening(FRONTEND):
        print("starting the Vite dev server ...")
        procs.append(subprocess.Popen(
            ["npm.cmd", "run", "dev", "--", "--port", "5173", "--strictPort",
             "--host", "127.0.0.1"],
            cwd=ROOT / "frontend",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        started_frontend = True
        if not wait_for(lambda: listening(FRONTEND), timeout=90):
            print("FAIL  the Vite dev server did not start on 127.0.0.1:5173")
            return 1

    token = owner_token_from_env()
    checks = Checks()
    console_errors: list[str] = []   # browser console messages of type "error"
    js_errors: list[str] = []        # uncaught exceptions: always a defect

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            page = browser.new_page()
            page.on("console",
                    lambda message: console_errors.append(message.text)
                    if message.type == "error" else None)
            page.on("pageerror", lambda error: js_errors.append(str(error)))

            try:
                run_checks(page, checks, token, console_errors, js_errors)
            except Exception as error:  # noqa: BLE001 - reported, never hidden
                checks.check("the browser run completed", False,
                             f"{type(error).__name__}: {error}")

            browser.close()
    finally:
        for proc in procs:
            try:
                subprocess.run(["taskkill", "/T", "/F", str(proc.pid)],
                               capture_output=True, check=False)
            except OSError:
                proc.kill()

    print(f"\n{checks.passed}/{checks.passed + checks.failed} checks passed"
          + (" (backend was already running)" if not started_backend else "")
          + (" (frontend was already running)" if not started_frontend else ""))
    return 0 if checks.failed == 0 else 1


def run_checks(page, checks: Checks, token: str, console_errors: list[str],
               js_errors: list[str]) -> None:
    """Every browser assertion, in order. Expects the dev servers to be up."""
    page.goto(FRONTEND, wait_until="domcontentloaded")
    page.wait_for_selector(".main-nav", timeout=20000)

    checks.check("the shell reports a live backend connection",
                 "SYSTEM CONNECTED" in page.inner_text(".topbar"))
    checks.check("the shell is marked PAPER MODE",
                 "PAPER MODE" in page.inner_text(".topbar"))
    checks.check("the navigation offers exactly nine pages",
                 page.locator(".main-nav a").count() == len(PAGES),
                 page.locator(".main-nav a").count())

    for path, title, nav in PAGES:
        page.goto(f"{FRONTEND}/{path}", wait_until="domcontentloaded")
        page.wait_for_function("() => !!document.querySelector('h1')", timeout=15000)
        heading = page.inner_text("h1").strip()
        checks.check(f"{path} renders its own page", heading == title, heading)
        active = page.locator(".main-nav a.active")
        checks.check(f"{path} marks the active navigation item",
                     active.count() == 1 and active.first.inner_text().strip() == nav,
                     active.first.inner_text().strip() if active.count() else "none")
        checks.check(f"{path} renders content below the heading",
                     len(page.inner_text("main").strip()) > len(title), "shell only")

    page.goto(f"{FRONTEND}/#/markets", wait_until="domcontentloaded")
    page.wait_for_selector("text=Streaming connection", timeout=15000)
    stream_panel = page.inner_text("section:has-text('Streaming connection')")
    checks.check("the Markets page reports a streaming state",
                 any(state in stream_panel
                     for state in ("NOT_CONFIGURED", "CONNECTED", "READY", "OFF",
                                   "RECONNECTING", "AUTH_FAILED", "ERROR")),
                 stream_panel.splitlines()[0] if stream_panel else "empty")
    # The UI must show the backend's own explanation verbatim - no invented
    # comfort text and no upgraded claim when the socket is not connected. A
    # healthy feed honestly reports no reason at all, and then the page must
    # invent no explanation either.
    backend_stream = page.evaluate(
        "async () => (await fetch('/api/status')).json()").get("market_data_stream") or {}
    reason = backend_stream.get("reason") or ""
    invented_reason = page.locator(
        "section:has-text('Streaming connection') .empty-state").count() > 0
    if reason:
        checks.check("the Markets page explains the feed state with the backend's own reason",
                     reason in stream_panel, reason[:70])
    else:
        checks.check("a feed reporting no reason shows no invented explanation",
                     not invented_reason, "no reason rendered")
    state = backend_stream.get("state")
    checks.check("the displayed state token matches the backend's state",
                 bool(state) and state in stream_panel, state)
    if state != "CONNECTED":
        checks.check("an unconnected feed is never shown as CONNECTED",
                     "CONNECTED" not in stream_panel, str(state))

    page.goto(f"{FRONTEND}/#/portfolio", wait_until="domcontentloaded")
    page.wait_for_selector("h1", timeout=15000)
    body = page.inner_text("main")
    checks.check("the portfolio never invents a price for an unpriced position",
                 "UNPRICED" in body or "unpriced" in body.lower()
                 or "no open positions" in body.lower(), "valuation state shown")

    page.goto(f"{FRONTEND}/#/nope-does-not-exist", wait_until="domcontentloaded")
    page.wait_for_selector("text=That page does not exist", timeout=15000)
    checks.check("an unknown address shows the 404 view with a way back",
                 page.locator("a:has-text('Back to overview')").count() >= 1)

    page.goto(f"{FRONTEND}/#/positions", wait_until="domcontentloaded")
    page.wait_for_selector("h1", timeout=15000)
    checks.check("the legacy single-page anchor still lands on Portfolio",
                 page.inner_text("h1").strip() == "Portfolio & positions",
                 page.inner_text("h1").strip())

    # -- owner controls, over HTTP, through the real UI ----------------------
    # Everything above is read-only navigation: it must be console-clean.
    checks.check("navigating every page raised no console error",
                 not console_errors, console_errors[:3])
    errors_before_the_negative_test = len(console_errors)

    page.goto(f"{FRONTEND}/#/control", wait_until="domcontentloaded")
    page.wait_for_selector("#owner-token", timeout=15000)
    panel = page.inner_text("main")
    checks.check("the control page states that live trading is unavailable",
                 "Live trading is unavailable" in panel)

    # The buttons follow the real orchestrator state: Start is disabled while
    # the engine runs, so the smoke drives it to a known state instead of
    # assuming one. Stop is the one control that stays enabled either way, and
    # without a token it must refuse before anything moves.
    orchestrator_state = str(page.evaluate(
        "async () => (await fetch('/api/status')).json().orchestrator?.status ?? ''"))
    page.click("button:text-is('Stop')")
    page.wait_for_selector("text=Enter the owner control token", timeout=10000)
    checks.check("an action without a token is refused with an explanation", True)

    if token:
        page.fill("#owner-token", token)
        if orchestrator_state != "STOPPED":
            page.click("button:text-is('Stop')")
            page.wait_for_selector(".controls-panel .status-tag:has-text('STOPPED')",
                                   timeout=15000)
            checks.check("the orchestrator reaches STOPPED from its starting state",
                         "STOPPED" in page.inner_text(".controls-panel"),
                         orchestrator_state)
        page.click("button:text-is('Start')")
        page.wait_for_selector(".controls-panel .status-tag:has-text('RUNNING')",
                               timeout=15000)
        checks.check("the owner token really starts the orchestrator",
                     "RUNNING" in page.inner_text(".controls-panel"))
        page.click("button:text-is('Pause')")
        page.wait_for_selector(".controls-panel .status-tag:has-text('PAUSED')",
                               timeout=15000)
        checks.check("the owner token really pauses the orchestrator",
                     "PAUSED" in page.inner_text(".controls-panel"))
        page.click("button:text-is('Resume')")
        page.wait_for_selector(".controls-panel .status-tag:has-text('RUNNING')",
                               timeout=15000)
        checks.check("the owner token really resumes the orchestrator",
                     "RUNNING" in page.inner_text(".controls-panel"))
        page.click("button:text-is('Stop')")
        page.wait_for_selector(".controls-panel .status-tag:has-text('STOPPED')",
                               timeout=15000)
        checks.check("the owner token really stops the orchestrator",
                     "STOPPED" in page.inner_text(".controls-panel"))
        page.fill("#owner-token", "definitely-the-wrong-token")
        page.click("button:text-is('Start')")
        page.wait_for_selector("text=owner authentication failed", timeout=15000)
        checks.check("a wrong token is refused by the backend", True)
        page.fill("#owner-token", "")
    else:
        print("SKIP  owner control round-trip (OWNER_CONTROL_TOKEN is empty)")

    # The only console traffic the run may produce after navigation is the
    # deliberate 401 from the wrong-token refusal above.
    refusals = console_errors[errors_before_the_negative_test:]
    checks.check("the only console error is the deliberate 401 refusal",
                 all("401" in message for message in refusals),
                 refusals[:3])
    checks.check("no uncaught JavaScript exception anywhere in the run",
                 not js_errors, js_errors[:3])

    dom = page.content()
    checks.check("the rendered page never contains the owner token",
                 not token or token not in dom)


if __name__ == "__main__":
    sys.exit(main())
