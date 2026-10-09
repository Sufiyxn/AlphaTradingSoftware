# Alpha Trading Platform

Local foundation for the broker-agnostic Alpha Trading Platform. The runtime is **disconnected by default**: it holds no synthetic market data, no demo history, no fabricated account, and no generated price movement. Paper mode simulates execution and account only - cash, fills, positions, and P&L - and every price comes from a connected provider or a TradingView signal. Nothing is connected until you configure it. The test analyzer is a pipeline fixture, not a trading strategy or evidence of profitability.

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
- `backend/config/providers.py` - market data, broker, TradingView, AI, and notification configuration.
- `backend/config/catalog.py` - the integration catalog: which providers are known, and which are actually implemented.
- `backend/config/loader.py` - `load_config()` / `load_settings()`; `backend/core/config.py` re-exports them.

Two rules the loader enforces at startup instead of at request time:

- **Secrets never leave the process.** Every configuration payload returned by the API is built through `describe()`, which reports credential *names* and `configured` / `available` booleans only. No key, token, or webhook URL value is ever included.
- **Configuration can only tighten risk.** `RISK_*` and `STRATEGY_*` values outside the shipped safety range raise `ValueError` on load. In particular `min_reward_risk` and `STRATEGY_MIN_REWARD_RISK` cannot drop below 2.0, `max_daily_loss_fraction` cannot exceed 0.10, `risk_fraction` and `max_risk_per_trade` cannot exceed 0.05, and `MAX_CANDIDATE_PRICE` cannot exceed 100. Phase 8 added hard ceilings in the risk layer itself: `RISK_MAX_QUANTITY` (default 10000, ceiling 100000), `RISK_MAX_SYMBOL_EXPOSURE` (25000), and `RISK_MAX_ENTRY_PRICE` (100, may only tighten `MAX_CANDIDATE_PRICE`), plus the opt-in `RISK_REQUIRE_FRESH_VALUATION`.

Trading mode comes from `TRADING_MODE` (`BACKTEST`, `PAPER`, `LIVE`). Selecting `LIVE` additionally requires `LIVE_TRADING_ENABLED=true`; without it the effective mode stays `PAPER` and the reason is reported. `MOCK`, `TEST`, `LOCAL`, and `FIXTURE` values for provider settings normalise to `none`, so a stale development `.env` cannot quietly re-enable a fixture as a live data source.

A fresh clone with an empty `.env` boots **fully disconnected**: no market data provider, no broker, no TradingView secret, no positions. Configuring a provider is an explicit act, and anything not configured reports `NOT_CONFIGURED` for the affected feature rather than failing the whole request.

## Local API

- `GET /api/health`, `/api/status`, `/api/configuration`, `/api/capabilities`
- `GET /api/opportunities`, `/api/quotes`, `/api/positions`, `/api/trades`
- `GET /api/performance`, `/api/reports`, `/api/logs`, `/api/orders`
- Authenticated `POST /api/control/start`, `/stop`, `/pause`, `/resume`, `/emergency-stop`, `/reset-emergency-stop`, `/terminate`, `/configuration`
- Authenticated `POST /api/orders/cancel`, `/api/orders/reconcile`, `/api/positions/close`
- `POST /api/webhooks/tradingview` (its own shared secret, no owner token required)

Reports can be generated on demand and are also written by a local scheduler after the US regular session. The current notifier is an in-process local sink; it does not send email, SMS, or third-party messages. `GET /api/logs` supports `kind`, `format=json|csv`, inclusive `start`/`end` dates, and `symbol` filters.

The displayed US session uses standard weekday, daylight-saving, holiday, and early-close rules from the Python standard library. One-off exchange closures are not represented.

## TradingView webhook

`POST /api/webhooks/tradingview` accepts TradingView alerts when `TRADINGVIEW_ENABLED=true` and `TRADINGVIEW_WEBHOOK_SECRET` is set. The endpoint authenticates with an `X-Webhook-Signature: sha256=<hex>` HMAC of the request body (or a `secret` field in the payload), never with the owner token, and never echoes the secret. Each delivery is validated and normalized (`backend/signals/`), checked for duplicates by body hash and explicit event id, then handed to the canonical trading pipeline (`docs/PIPELINE.md`) - connection verification, `RiskEngine`, execution safety gate, selected adapter - and there is no direct TradingView to broker execution.

Responses are machine-readable (`202 ACCEPTED`, `400 INVALID_PAYLOAD`, `401 AUTH_FAILED`, `409 DUPLICATE_SIGNAL`, `413 PAYLOAD_TOO_LARGE`, `422 RISK_REJECTED` and related business rejections, `503 NOT_CONFIGURED`/`EXECUTION_UNAVAILABLE`, `500 INTERNAL_ERROR`). The alert price is signal/entry metadata only: it never becomes a position's ongoing market price, so a position opened this way stays `UNPRICED` until a real market-data provider is connected. Full payload schema, aliases, validation rules, and connection states are documented in `docs/TRADINGVIEW.md`.

## Data and trading limits

The product ships with **no market data connected**. `GET /api/status` reports `market_data_state: NOT_CONFIGURED`, the scanner returns no candidates, and no position is opened. Nothing is substituted in their place: there is no bundled history, no sample series, and no synthetic candle anywhere in the runtime.

Connect one of the documented providers in `.env` to supply prices (configuration, states, and error mapping are documented in `docs/MARKET_DATA.md`):

- a configured market-data provider (Polygon, Alpha Vantage) for live and historical prices;
- TradingView alerts delivered to the webhook endpoint for signals.

Each provider reads only its own key (`POLYGON_API_KEY`, `ALPHAVANTAGE_API_KEY`), and credentials alone never count as connectivity: the backend records real evidence per request - `UNAVAILABLE` until the first success, then `CONNECTED`, downgraded to `STALE` after the freshness window, `RATE_LIMITED` while throttled, or `ERROR` when credentials are rejected or a response breaks the documented schema. `GET /api/backtest` runs only on bars actually retrieved from the connected provider and answers `status: NO_DATA` when there are none, rather than claiming a result.

Paper trading then simulates **execution and account only** - simulated cash (`PAPER_STARTING_CASH`), simulated fills, simulated positions, and simulated P&L computed from those real prices. A paper position opened with no continuous price source is marked `UNPRICED` and is never marked, stopped, or targeted from an invented price; stop-loss and take-profit act only once a real price feed is connected. Manual closure stays available and always requires an explicit real exit price.

A market-data bridge (`backend/market_data/bridge.py`) is the only path that moves a paper position's market price. It is provider-agnostic: it reads whatever `MarketDataProvider` is connected, ages each quote against `MARKET_DATA_STALE_AFTER_SECONDS` (default 300), and reports one of five availability states on `/api/status` and `/api/positions` - `CONNECTED`, `NOT_CONFIGURED`, `UNAVAILABLE`, `STALE`, or `ERROR`. A position is `FRESH` only when its mark came from a quote inside that window; `STALE` marks keep the real observed price and timestamp but pause automatic exits; `UNPRICED` marks report `current_price: null` and `unrealized_pnl: null` so no consumer can render a fabricated $0. `GET /api/positions` also reports `price_state`, `price_as_of`, `price_reason`, `auto_exit_enabled`, and `portfolio.valuation_status` (`COMPLETE` or `PARTIAL`).

Because the portfolio is in-memory but every trade is journaled, `ApplicationRuntime` reconciles the two at startup (`backend/paper_trading/reconcile.py`): open positions, realized P&L, trade counts, and cash are rebuilt from SQLite, restored positions start `UNPRICED`, and a `JOURNAL_RECONCILED` event is written. A restart cannot silently diverge from the recorded trade book.

Deterministic fixtures used by the automated tests live in `backend/testing`. `ApplicationRuntime` never imports them; passing a fixture into the runtime happens only from a test.

The **Connections & Live Setup** section reports configuration presence for market data, platform, AI, notifications, TradingView, and execution providers. Each section reports `configured` (credentials supplied), `implemented` (this build ships the adapter), and `state` (`CONFIGURED` or `NOT_CONFIGURED`), and a connection test returns `NOT_CONFIGURED` rather than a pass when nothing is connected. When a market-data provider is configured, its connection test runs a real probe **in the backend** and the card shows the recorded connectivity (`CONNECTED`, `STALE`, `RATE_LIMITED`, `UNAVAILABLE`, `ERROR`) with its reason; the browser never contacts a vendor. TradingView additionally reports a live webhook state - `NOT_CONFIGURED`, `UNAVAILABLE`, `CONNECTED`, `STALE`, or `ERROR` - that only reaches `CONNECTED` after an authenticated signal has actually been processed; its connection test is a local authentication self-check. Secret values are never returned by the API or stored in SQLite. Live mode is locked until the live gate opens.

The strategy/risk/backtest modules are available for local development. Risk remains between strategy evaluation and paper execution, including strict entry below $100, 2:1 minimum reward/risk, per-trade and daily loss limits, exposure/position limits, and the kill switch. Configuration can only tighten those limits, never widen them. Since Phase 8 the risk layer is the single deterministic boundary every intent passes exactly once: constraint-based position sizing, per-trade risk caps, 2:1 reward/risk, price sanity and direction checks, portfolio and per-symbol exposure limits, duplicate-symbol control, the UTC daily-loss stop, and the kill switch - each rejection carrying a machine-readable code and reason, with a full audit record (`RiskDecision`, entry/stop/target, requested vs approved quantity, exposures, equity, active limits) journaled so any decision can be replayed from the journal alone. Details and formulas: `docs/PIPELINE.md`. Performance qualification requires a sufficient evaluation sample and at least 51% measured win rate. Fixture results are not a profitability claim or guarantee.

## Execution connections

Execution is provider-agnostic (`docs/CONNECTIONS.md`). Every order - a TradingView alert, an orchestrator cycle, or an internal submission - travels the one canonical pipeline (`docs/PIPELINE.md`): intent validation, signal freshness, connection verification through the shared `ConnectionManager`, `RiskEngine`, the execution safety gate, mode/capability checks, then the verified adapter, with a deterministic outcome code and one journal record per decision. Each order is then registered in the canonical order lifecycle (`docs/ORDERS.md`): a platform-owned `client_order_id`, a `provider_order_id` taken verbatim from the provider, one state machine over submitted/accepted/partially-filled/filled/cancelled/rejected/expired, and executed quantity always derived from the recorded fills. An order that is accepted but not yet filled is reported as `SUBMITTED` (not a rejection) and reserves risk capacity - duplicate-symbol, max-position, and exposure limits count it - until it fills or is cancelled. `GET /api/orders` lists them, `POST /api/orders/cancel` cancels one, and `POST /api/orders/reconcile` compares the journal with the provider read-only, reporting `PROVIDER_ERROR`/`INCOMPLETE` as unresolved rather than as a clean match. The runtime ships one implemented provider - the in-process `local_paper` account - and advertises external providers such as Alpaca and Robinhood as *not installed*; configuring their credentials reports `CONFIGURED`, never `CONNECTED`, and a connection test answers "not installed; no network request was made" instead of inventing a result. Credentials alone are never connectivity: a connection reaches `CONNECTED` only after authentication, account verification, and permission checks actually succeeded inside the freshness window, and the dashboard shows states and reasons only - never a secret value. Multiple client connections are tracked independently, capability limits (order types, short selling, live trading) are reported as limitations rather than assumed support, and the execution safety gate keeps `LIVE` locked while the risk kill switch and emergency stop stay between every decision and execution.

## Verification

```powershell
python -m pytest backend/tests -q
cd frontend
npm run build
```

Expected: `467 passed, 150 subtests` on the backend (including `backend/tests/test_risk_hardening.py` and `backend/tests/test_order_lifecycle.py`), and a successful frontend type-check/build with no changes required for Phase 9 (orders travel the existing API surface).

SQLite journal data is stored under `data/alpha_trading.sqlite3` by default and is ignored by Git. Override `DATABASE_PATH` to use a different local file. Secrets belong only in `.env`; `.env` is ignored and `.env.example` contains no credentials.
