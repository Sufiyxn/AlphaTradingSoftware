# Alpha Trading Platform

Local foundation for the broker-agnostic Alpha Trading Platform. The runtime is **disconnected by default**: it holds no synthetic market data, no demo history, no fabricated account, and no generated price movement. Paper mode simulates execution and account only - cash, fills, positions, and P&L - and every price comes from a connected provider. Nothing is connected until you configure it. The test analyzer is a pipeline fixture, not a trading strategy or evidence of profitability.

## Start locally

Requirements: Python 3.11+, Node.js, and npm. No Python runtime packages need installing.

Backend, from the repository root:

```powershell
Copy-Item .env.example .env
# Set a unique OWNER_CONTROL_TOKEN in .env to enable authenticated controls.
python -m backend
```

Frontend, in another terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open the Vite URL printed in the terminal (normally `http://localhost:5173`). The Vite development server proxies `/api` to `http://127.0.0.1:8000`.

Owner controls are disabled when `OWNER_CONTROL_TOKEN` is empty. Enter the configured token in the dashboard to use Start, Pause, Stop, emergency stop, and emergency-stop reset. Emergency stop latches the risk kill switch; resetting it is an explicit owner action and returns the orchestrator to STOPPED. The backend binds to `127.0.0.1` by default. Keep it local; do not expose owner controls on an untrusted network.

## Configuration

Settings are read in one place, `backend/config`, from the process environment plus a local `.env` file. Process variables always win over the file; the file never overwrites them.

- `backend/config/settings.py` - server and trading settings.
- `backend/config/providers.py` - market data, broker, AI, and notification configuration.
- `backend/config/catalog.py` - the integration catalog: which providers are known, and which are actually implemented.
- `backend/config/loader.py` - `load_config()` / `load_settings()`; `backend/core/config.py` re-exports them.

Two rules the loader enforces at startup instead of at request time:

- **Secrets never leave the process.** Every configuration payload returned by the API is built through `describe()`, which reports credential *names* and `configured` / `available` booleans only. No key, token, or webhook URL value is ever included.
- **Configuration can only tighten risk.** `RISK_*` and `STRATEGY_*` values outside the shipped safety range raise `ValueError` on load. In particular `min_reward_risk` and `STRATEGY_MIN_REWARD_RISK` cannot drop below 2.0, `max_daily_loss_fraction` cannot exceed 0.10, `risk_fraction` and `max_risk_per_trade` cannot exceed 0.05, and `MAX_CANDIDATE_PRICE` cannot exceed 100. Phase 8 added hard ceilings in the risk layer itself: `RISK_MAX_QUANTITY` (default 10000, ceiling 100000), `RISK_MAX_SYMBOL_EXPOSURE` (25000), and `RISK_MAX_ENTRY_PRICE` (100, may only tighten `MAX_CANDIDATE_PRICE`), plus the opt-in `RISK_REQUIRE_FRESH_VALUATION`.

Trading mode comes from `TRADING_MODE` (`BACKTEST`, `PAPER`, `LIVE`). Selecting `LIVE` additionally requires `LIVE_TRADING_ENABLED=true`; without it the effective mode stays `PAPER` and the reason is reported. `MOCK`, `TEST`, `LOCAL`, and `FIXTURE` values for provider settings normalise to `none`, so a stale development `.env` cannot quietly re-enable a fixture as a live data source.

A fresh clone with an empty `.env` boots **fully disconnected**: no market data provider, no broker, no positions. Configuring a provider is an explicit act, and anything not configured reports `NOT_CONFIGURED` for the affected feature rather than failing the whole request.

## Local API

- `GET /api/health`, `/api/status`, `/api/configuration`, `/api/capabilities`
- `GET /api/opportunities`, `/api/quotes`, `/api/positions`, `/api/trades`
- `GET /api/performance`, `/api/reports`, `/api/logs`, `/api/orders`
- Authenticated `POST /api/control/start`, `/stop`, `/pause`, `/resume`, `/emergency-stop`, `/reset-emergency-stop`, `/terminate`, `/configuration`
- Authenticated `POST /api/orders/cancel`, `/api/orders/reconcile`, `/api/positions/close`, `/api/positions/risk`

Reports can be generated on demand and are also written by a local scheduler after the US regular session. The current notifier is an in-process local sink; it does not send email, SMS, or third-party messages. `GET /api/logs` supports `kind`, `format=json|csv`, inclusive `start`/`end` dates, and `symbol` filters.

The displayed US session uses standard weekday, daylight-saving, holiday, and early-close rules from the Python standard library. One-off exchange closures are not represented. Since Phase 11 that same calendar also gates execution and scanning: a new order submitted outside the allowed session is refused with `SESSION_BLOCKED` (no order is created), and the orchestrator skips its scan cycle entirely (`CYCLE_SESSION_SKIPPED`, never an error) so no candidate is evaluated when no entry could be acted on. `GET /api/status` reports `market_session_policy` (`regular` by default, `extended` via `MARKET_SESSION_SUBMISSION`), `market_session_orders_allowed`, and the orchestrator's `session_blocked` / `session_reason`. Weekends and market holidays are refused under either policy; the `extended` policy additionally requires an execution provider that declares extended-hours capability. Positions keep aging honestly while the market is closed - a real quote older than the staleness window is reported `STALE` with its true price and timestamp, never kept `FRESH` and never invented. Exits and owner-authorized closes stay available — they are paper-ledger position actions, not venue orders.

## Signal submissions

There is no signal vendor in this build: the TradingView webhook integration (route, HMAC
authenticator, signal validation layer, connection card, `docs/TRADINGVIEW.md`) was removed on
2026-10-12, and `POST /api/webhooks/tradingview` now answers `404`. Signals enter the platform as
trade setups handed to the canonical pipeline (`docs/PIPELINE.md`): `PaperTradingEngine.submit()`
accepts a `TradeSetup` plus a source, a signal id, an optional freshness window and an
idempotency key, and runs the same sequence every other order travels - intent validation, signal
freshness, connection verification, `RiskEngine`, the execution safety gate, mode/capability
checks, then the verified paper adapter. The outcome is one deterministic code (`ACCEPTED`,
`DUPLICATE_INTENT`, `RISK_REJECTED`, `SESSION_BLOCKED`, ...), journaled with the rest of the
audit trail, and a replayed idempotency key never books a second order. The scanner is the
built-in signal source: each orchestrator cycle produces setups from real market data and submits
them through exactly this path.

## Data and trading limits

The product ships with **no market data connected**. `GET /api/status` reports `market_data_state: NOT_CONFIGURED`, the scanner returns no candidates, and no position is opened. Nothing is substituted in their place: there is no bundled history, no sample series, and no synthetic candle anywhere in the runtime.

Connect one of the documented providers in `.env` to supply prices (configuration, states, and error mapping are documented in `docs/MARKET_DATA.md`):

- a configured market-data provider (Polygon, Alpha Vantage, Alpaca) for live and historical prices;
- a live WebSocket stream from Polygon or Alpaca that pushes trades to the charts, watchlist, scanner, and valuation through the same quote service (see below).

Each provider reads only its own key (`POLYGON_API_KEY`, `ALPHAVANTAGE_API_KEY`, `ALPACA_API_KEY`/`ALPACA_API_SECRET`), and credentials alone never count as connectivity: the backend records real evidence per request - `UNAVAILABLE` until the first success, then `CONNECTED`, downgraded to `STALE` after the freshness window, `RATE_LIMITED` while throttled, or `ERROR` when credentials are rejected or a response breaks the documented schema. `GET /api/backtest` runs only on bars actually retrieved from the connected provider and answers `status: NO_DATA` when there are none, rather than claiming a result.

Paper trading then simulates **execution and account only** - simulated cash (`PAPER_STARTING_CASH`), simulated fills, simulated positions, and simulated P&L computed from those real prices. A paper position opened with no continuous price source is marked `UNPRICED` and is never marked, stopped, or targeted from an invented price; stop-loss and take-profit act only once a real market-data provider is connected. Manual closure stays available, always requires an explicit real exit price, and can scale a position out in part instead of only closing it in full.

A market-data bridge (`backend/market_data/bridge.py`) is the only path that moves a paper position's market price. It is provider-agnostic: it reads whatever `MarketDataProvider` is connected, ages each quote against `MARKET_DATA_STALE_AFTER_SECONDS` (default 300), and reports one of five availability states on `/api/status` and `/api/positions` - `CONNECTED`, `NOT_CONFIGURED`, `UNAVAILABLE`, `STALE`, or `ERROR`. A position is `FRESH` only when its mark came from a quote inside that window; `STALE` marks keep the real observed price and timestamp but pause automatic exits; `UNPRICED` marks report `current_price: null` and `unrealized_pnl: null` so no consumer can render a fabricated $0. `GET /api/positions` also reports `price_state`, `price_as_of`, `price_reason`, `auto_exit_enabled`, and `portfolio.valuation_status` (`COMPLETE` or `PARTIAL`).

Live prices can also arrive as a **push stream** instead of a REST refresh: with `MARKET_DATA_STREAM=on` and a Polygon or Alpaca provider configured, the backend opens one WebSocket (a stdlib RFC 6455 client, `backend/market_data/ws.py`), authenticates, subscribes to the trade channel for the current universe, and normalizes every event with its provider timestamp into the same `MarketDataService` that feeds charts, the watchlist, the scanner, and valuation. `GET /api/status`, `GET /api/quotes`, and the Markets page report the socket's honest state (`READY`, `CONNECTING`, `CONNECTED`, `RECONNECTING`, `AUTH_FAILED`, `SUBSCRIPTION_FAILED`, `NOT_CONFIGURED`, `OFF`), the feed entitlement (for example Alpaca's free plan = real-time IEX), event counters, the last event time, and the reconnect count; `streaming: true` is only ever reported for a live socket. A dropped socket reconnects with capped exponential backoff, and a symbol the stream has not seen falls back to the REST provider or stays `UNPRICED`. No test can open a vendor socket: the stream is exercised against the loopback harness `backend/testing/ws_server.py` and `python scripts/stream_smoke.py` proves the whole path over a real socket without contacting any vendor.

Because the portfolio is in-memory but every trade is journaled, `ApplicationRuntime` reconciles the two at startup (`backend/paper_trading/reconcile.py`): open positions, realized P&L, trade counts, and cash are rebuilt from SQLite, restored positions start `UNPRICED`, and a `JOURNAL_RECONCILED` event is written. A restart cannot silently diverge from the recorded trade book.

Deterministic fixtures used by the automated tests live in `backend/testing`. `ApplicationRuntime` never imports them; passing a fixture into the runtime happens only from a test.

The **Connections & Live Setup** section reports configuration presence for market data, platform, AI, notifications, and execution providers. Each section reports `configured` (credentials supplied), `implemented` (this build ships the adapter), and `state` (`CONFIGURED` or `NOT_CONFIGURED`), and a connection test returns `NOT_CONFIGURED` rather than a pass when nothing is connected. When a market-data provider is configured, its connection test runs a real probe **in the backend** and the card shows the recorded connectivity (`CONNECTED`, `STALE`, `RATE_LIMITED`, `UNAVAILABLE`, `ERROR`) with its reason; the browser never contacts a vendor. Secret values are never returned by the API or stored in SQLite. Live mode is locked until the live gate opens.

The strategy/risk/backtest modules are available for local development. Risk remains between strategy evaluation and paper execution, including strict entry below $100, 2:1 minimum reward/risk, per-trade and daily loss limits, exposure/position limits, and the kill switch. Configuration can only tighten those limits, never widen them. Since Phase 8 the risk layer is the single deterministic boundary every intent passes exactly once: constraint-based position sizing, per-trade risk caps, 2:1 reward/risk, price sanity and direction checks, portfolio and per-symbol exposure limits, duplicate-symbol control, the UTC daily-loss stop, and the kill switch - each rejection carrying a machine-readable code and reason, with a full audit record (`RiskDecision`, entry/stop/target, requested vs approved quantity, exposures, equity, active limits) journaled so any decision can be replayed from the journal alone. Details and formulas: `docs/PIPELINE.md`. Performance qualification requires a sufficient evaluation sample and at least 51% measured win rate. Fixture results are not a profitability claim or guarantee.

## Execution connections

Execution is provider-agnostic (`docs/CONNECTIONS.md`). Every order - a scanner cycle, an owner-authorized submission, or an internal one - travels the one canonical pipeline (`docs/PIPELINE.md`): intent validation, signal freshness, connection verification through the shared `ConnectionManager`, `RiskEngine`, the execution safety gate, mode/capability checks, then the verified adapter, with a deterministic outcome code and one journal record per decision. Each order is then registered in the canonical order lifecycle (`docs/ORDERS.md`): a platform-owned `client_order_id`, a `provider_order_id` taken verbatim from the provider, one state machine over submitted/accepted/partially-filled/filled/cancelled/rejected/expired, and executed quantity always derived from the recorded fills. An order that is accepted but not yet filled is reported as `SUBMITTED` (not a rejection) and reserves risk capacity - duplicate-symbol, max-position, and exposure limits count it - until it fills or is cancelled. `GET /api/orders` lists them, `POST /api/orders/cancel` cancels one, and `POST /api/orders/reconcile` compares the journal with the provider read-only, reporting `PROVIDER_ERROR`/`INCOMPLETE` as unresolved rather than as a clean match. The runtime ships one implemented provider - the in-process `local_paper` account - and advertises external providers such as Alpaca and Robinhood as *not installed*; configuring their credentials reports `CONFIGURED`, never `CONNECTED`, and a connection test answers "not installed; no network request was made" instead of inventing a result. Credentials alone are never connectivity: a connection reaches `CONNECTED` only after authentication, account verification, and permission checks actually succeeded inside the freshness window, and the dashboard shows states and reasons only - never a secret value. Multiple client connections are tracked independently, capability limits (order types, short selling, live trading) are reported as limitations rather than assumed support, and the execution safety gate keeps `LIVE` locked while the risk kill switch and emergency stop stay between every decision and execution.

## Verification

```powershell
python -m pytest backend/tests -q
python scripts/http_smoke.py         # 58 checks against a running backend
python scripts/stream_smoke.py       # 13 checks, real socket, no vendor contact
python scripts/e2e_smoke.py          # 48 browser checks (starts dev servers as needed)
cd frontend
npm run build                        # tsc --noEmit + vite build
npm test                             # node --test
```

Expected: `632 passed, 256 subtests` on the backend, `58/58`, `13/13`, `48/48` for the smoke scripts, and a clean frontend type-check/build. Frontend tests: `21 passed` when the optional live check is enabled (`$env:ALPHA_API_BASE='http://127.0.0.1:8000'; npm test` with a backend running), otherwise `20 passed, 1 skipped`. The browser smoke needs Playwright's Chromium once (`python -m playwright install chromium`).

SQLite journal data is stored under `data/alpha_trading.sqlite3` by default and is ignored by Git. Override `DATABASE_PATH` to use a different local file. Secrets belong only in `.env`; `.env` is ignored and `.env.example` contains no credentials.
