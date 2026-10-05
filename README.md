# Alpha Trading Platform

Local V1 foundation for the broker-agnostic Alpha Trading Platform. The current runtime is paper-only and uses deterministic mock market data and a local mock execution adapter. It does not connect to a market vendor or broker, and it cannot place live orders. The test analyzer is a pipeline fixture, not a trading strategy or evidence of profitability.

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

## Local API

- `GET /api/health`, `/api/status`, `/api/configuration`, `/api/capabilities`
- `GET /api/opportunities`, `/api/quotes`, `/api/positions`, `/api/trades`
- `GET /api/performance`, `/api/reports`, `/api/logs`
- Authenticated `POST /api/control/start`, `/stop`, `/pause`, `/resume`, `/emergency-stop`, `/reset-emergency-stop`, `/terminate`, `/configuration`

Reports can be generated on demand and are also written by a local scheduler after the US regular session. The current notifier is an in-process local sink; it does not send email, SMS, or third-party messages. `GET /api/logs` supports `kind`, `format=json|csv`, inclusive `start`/`end` dates, and `symbol` filters.

The displayed US session uses standard weekday, daylight-saving, holiday, and early-close rules from the Python standard library. One-off exchange closures are not represented.

## Data and trading limits

The mock provider contains a tiny fixed 2024 sample, so the displayed quotes are stale demonstration data. The scanner reports its temporary test analysis separately from the strategy engine. Insufficient history means no strategy setup; do not treat the sample as current market information. No public-market provider or TradingView alert endpoint is configured in this version. The execution router only registers the mock adapter, and status explicitly reports live trading unavailable.

The strategy/risk/backtest modules are available for local development. Risk remains between strategy evaluation and paper execution, including 2:1 minimum reward/risk, per-trade and daily loss limits, exposure/position limits, and the kill switch. The 51% realized win-rate value is an evaluation target, not a claim or guarantee.

## Verification

```powershell
python -m pytest backend/tests -q
cd frontend
npm run build
```

SQLite journal data is stored under `data/alpha_trading.sqlite3` by default and is ignored by Git. Override `DATABASE_PATH` to use a different local file. Secrets belong only in `.env`; `.env` is ignored and `.env.example` contains no credentials.
