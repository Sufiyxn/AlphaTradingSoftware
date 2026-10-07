# Alpha Trading Platform — Binding 24-Phase Roadmap

> **Approved.** Phases 1–5 are complete and verified. Phase 6 — *Connection & Execution Provider
> Framework* — is **NOT STARTED** and must not begin without explicit approval. Phases 6–24 are
> binding scope.
> **Do not start, reorder, or collapse phases without explicit approval.** Each phase ends only
> after the verification protocol below passes with exact numbers reported.
>
> *Documentation-only checkpoint update (post-Phase 5): Phase 5 is recorded as DONE, Phase 6 is
> renamed/reframed as the provider-agnostic "Connection & Execution Provider Framework" and stays
> NOT STARTED, and the pending phases were renumbered/expanded to cover the autonomous end goal.
> No Phase 5 or TradingView implementation code was changed by this update.*

---

## Binding constraints (apply to every phase)

1. **No fabricated market data, ever, in production.** No synthetic candles, ticks, prices, fills,
   P&L, demo symbols, fake balances, or fake trades. Test fixtures live only in `backend/testing/`
   and are never wired by `ApplicationRuntime`. `MOCK` is not a product mode;
   `backend/config/loader.py` normalizes `MOCK`/`TEST`/`FIXTURE` away for the running app.
   **No fabricated prices and no fabricated P&L.** When fresh real pricing is unavailable the
   platform reports `UNPRICED` / `NO_DATA` — never an invented value.
2. **One mandatory execution path — `TradingPipeline` is the canonical target architecture.**
   `TradingOrchestrator` is not a rename substitute for it. `TradingOrchestrator` stays functional
   through the early pending phases where practical; the proper `TradingPipeline`
   extraction/consolidation happens in **Phase 7 (Canonical Trading Pipeline)**. The eventual
   canonical flow:

   ```
   Signal Source (TradingView alert | internal strategy | AI advisory | future sources)
       -> Signal Normalizer / Validator
        -> TradingPipeline
             -> Strategy
             -> optional AI analysis/advisory
             -> Risk
             -> Portfolio Rules
             -> Execution Router (provider-agnostic)
        -> Mode-specific execution (paper | live via a verified execution provider)
   ```

   The frontend, TradingView, AI, and individual providers must never bypass this architecture.
   Until Phase 7 lands, every new signal path must still pass `RiskEngine` before execution —
   and that requirement never expires.
3. **Risk floors are not tunable downward.** `min_reward_risk >= 2.0`
   (`backend/strategies/models.py:34`, `backend/risk/config.py:25`),
   `qualification_win_rate >= 0.51` (`backend/backtesting/config.py:33`),
   `minimum_win_rate >= 51.0` (`backend/reporting/performance.py:57`).
4. **Single application clock.** `ApplicationRuntime` owns `clock`; services take `*clock` and never
   call `datetime.now()` directly.
5. **No vendor API calls during development or tests.** Adapters use stdlib `urllib` behind an
   injectable transport. Tests inject a fake transport; production code never reaches the network in
   CI or in the dev smoke.
6. **Secrets never leave the process.** No API key, token, or webhook secret in logs, API payloads,
   journal rows, exports, screenshots, or the frontend bundle. Status endpoints expose presence and
   state only. Secrets are never exposed, logged, or committed.
7. **HTTP auth split.** `POST /api/webhooks/tradingview` uses its dedicated webhook-secret/HMAC
   authentication and **must not** require the owner token. Every other state-changing `POST`
   requires `X-Owner-Token` with owner controls enabled.
8. **Data-dependent features degrade honestly.** Structured `NOT_CONFIGURED` / `UNAVAILABLE` with a
   reason — never a fabricated value, never a global error banner. Credential presence is not
   connectivity; provider health must be checked before anything is treated as usable.
9. **Working code is not rewritten.** Extend, wire, and test; do not re-architect passing systems.
10. **Integration choices.** Market data = **Polygon + Alpha Vantage** (locked, Phase 5, done).
    **Execution providers are deliberately NOT locked**: the platform is broker/provider agnostic
    (see *Execution is provider-agnostic* below). Robinhood and Alpaca are **examples** of
    execution platforms a client may use — they are not architectural requirements. Notifiers =
    Local, Generic Webhook, Discord, Telegram over stdlib HTTP. Charts = hand-rolled SVG (no chart
    library). Frontend tests = `vitest` + `jsdom` + `@testing-library/react`.
11. **The three systems stay independent.** Market-data providers, signal/analysis sources, and
    execution providers are separate architectural concepts and must never be coupled
    (TradingView → broker, AI → broker, strategy → broker, market-data provider → broker).
    `RiskEngine` always sits between decision generation and execution.
12. **TradingView is required for the initial testing/demo and remains first-class.** It is a
    signal/alert source only — never a broker, never the execution engine, never the continuous
    market-data provider. The Phase 4 implementation (HMAC auth, replay protection, signal
    validation, stale-signal handling, RiskEngine path, no direct TradingView → broker execution)
    must not be removed, weakened, bypassed, or redesigned, including after live execution lands.
13. **No automatic SL/TP on stale or fabricated prices.** Automatic exits require a `FRESH` real
    mark with `price_as_of` set (Phase 3 contract). Manual close never invents a price.
14. **Live execution is gated.** Live execution requires verified credentials/configuration, an
    explicitly verified and ready provider connection, explicit safety gates, and a
    kill-switch/emergency-stop path. Credentials alone never mean connectivity or readiness.
15. **Paper and live execution stay clearly separated**, and test mocks stay quarantined to test
    infrastructure (`backend/testing/`, `backend/tests/`) — never reachable from
    `ApplicationRuntime`.
16. **The end goal is an autonomous trading automation platform.** A dashboard, a backtester, a
    TradingView webhook receiver, and a paper-trading simulator are components/stages of that
    larger system — they are not the product.

### Per-phase verification protocol

- `python -m pytest backend/tests -q` (with `PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`) —
  count must not drop below the previous phase (262 as of Phase 5).
- `node node_modules\typescript\bin\tsc --noEmit -p tsconfig.json` in `frontend/`.
- `npm.cmd run build` in `frontend/`.
- HTTP smoke against a running instance: `/api/status`, `/api/positions`, `/api/readiness`,
  `/api/connections` parsed as JSON.
- `git diff` reviewed for accidental regressions.
- Explicit confirmation: no synthetic/demo data introduced, no `MOCK` reachable from the
  production runtime.
- Frontend tests, once Phase 23 lands.

---

## Architectural concepts (read before implementing any pending phase)

### Three independent systems

These are separate subsystems. They may share plumbing, but they must not be collapsed into one
another, and no decision or execution shortcut may join them directly.

| System | Members today | Role |
|---|---|---|
| **Market data providers** | Polygon, Alpha Vantage, future providers | Continuous, real pricing/history for scanning, valuation, marking, backtesting. Phases 3–5. |
| **Signal / analysis sources** | TradingView, internal strategies, future signal sources, AI advisory | Produce signals, setups, and advisory input. Nothing in this row executes an order. |
| **Execution providers** | Robinhood, Alpaca, future brokers/trading platforms | Submit/cancel/modify orders and report fills **through a standardized internal execution interface**, only after `RiskEngine`. Not built yet (Phase 6). |

Forbidden couplings — architectural, permanent:

- TradingView → broker
- AI → broker
- Strategy → broker
- Market data provider → broker

`RiskEngine` remains between decision generation and execution in every path.

### Execution is provider-agnostic (Phase 6 shape — documented, not implemented)

The platform must **not** be designed around Robinhood, Alpaca, or any single broker. Clients use
many different trading platforms/providers; the client supplies their own connection details and
credentials. The target flow:

```
Client connection credentials/details
  -> Connection Manager
  -> provider identification / configuration
  -> authentication
  -> capability discovery
  -> account / permission verification
  -> readiness checks
  -> execution provider adapter
  -> standardized internal execution interface
  -> trading automation
```

Rules:

- The trading engine, strategies, AI advisory, risk engine, and position management must contain
  **no broker-specific logic**. The client must not hard-code a broker into a trading strategy.
- If automatic provider identification is technically impossible, the UI asks the client to
  identify the provider. Arbitrary credentials are never assumed to be identifiable automatically.
- A client may configure **multiple execution connections** — Connection A → Robinhood,
  Connection B → Alpaca, Connection C → another supported provider. Automation runs against a
  verified/authorized connection per the client's configuration.
- Neither Robinhood nor Alpaca is mandatory. New providers must be addable **without modifying**
  the core trading strategy/decision engine.

### TradingView's role in the initial demonstration

TradingView **is required** for the initial testing and client-demonstration workflow, and must
remain supported after live execution is introduced. It is a signal/alert source — not a broker,
not the execution engine, not the continuous market-data provider.

Intended initial demonstration flow:

```
TradingView alert
  -> Alpha Trading Platform webhook
  -> signal normalization
  -> strategy / analysis
  -> RiskEngine
  -> paper execution
  -> real market-data valuation
  -> position management
  -> reporting / journal
```

Preserve from Phase 4: HMAC authentication, replay protection, signal validation, stale-signal
handling, the `RiskEngine` path, and the absence of any direct TradingView → broker execution.

### End goal

The final product is intended to become an **autonomous trading automation platform**. The
end-to-end conceptual flow:

```
real market data
  -> continuously scan / analyze
  -> identify valid opportunities
  -> strategy evaluation
  -> AI advisory where applicable
  -> risk validation
  -> trading decision
  -> verified execution connection
  -> execute
  -> monitor position
  -> dynamically manage position
  -> close
  -> reconcile
  -> journal
  -> performance / reporting
```

The platform is **not** merely a dashboard, a backtester, a TradingView webhook receiver, or a
paper-trading simulator. Those are components/stages of the larger system.

---

## The roadmap

| # | Phase | Status |
|---|---|---|
| 1 | Configuration / Mode Foundation | **DONE** |
| 2 | Remove Synthetic/Demo Production Runtime | **DONE** |
| 3 | Real Market-Data Bridge + Paper Position Lifecycle | **DONE** |
| 4 | TradingView Integration | **DONE** |
| 5 | Real Market-Data Providers | **DONE** |
| 6 | Connection & Execution Provider Framework | **NOT STARTED** |
| 7 | Canonical Trading Pipeline | pending |
| 8 | Risk Hardening | pending |
| 9 | Order Lifecycle Management & Reconciliation | pending |
| 10 | First-Class Position Management | pending |
| 11 | Market-Session / Market-Hours Handling | pending |
| 12 | Autonomous Decision Engine | pending |
| 13 | Strategy Evaluation & Anti-Overfitting Validation | pending |
| 14 | Backtesting Product Quality | pending |
| 15 | Connection Center | pending |
| 16 | Notifications | pending |
| 17 | 24/7 Worker / Operations Layer | pending |
| 18 | Monitoring / Health | pending |
| 19 | Reporting / Performance / Unified Clock | pending |
| 20 | Security / API Hardening | pending |
| 21 | Database / Persistence Hardening | pending |
| 22 | Dashboard / Frontend | pending |
| 23 | Testing / Dependencies / CI | pending |
| 24 | Final Integration / Production Readiness | pending |

> The completed sequence 1–5 is fixed. Phases 6–24 are all **pending**; their internal order may be
> refined only by an explicit, approved documentation update. **Phase 6 is the next phase and must
> remain NOT STARTED until explicitly approved.**

---

## Completed phases

### PHASE 1 — Configuration / Mode Foundation · DONE

- Added `backend/modes.py` (`TradingMode`, `ModeSelection`) and the `backend/config/` package:
  `settings.py`, `providers.py` (typed dataclasses + `describe()`), `loader.py` (`load_config`,
  `load_settings`, dev-only value normalization), `catalog.py` (integration catalogs with honest
  `implemented` flags).
- `backend/core/config.py` became a shim so existing importers kept working.
- `RiskConfig.__post_init__` enforces ceilings: `MAX_RISK_FRACTION=0.05`,
  `MAX_RISK_PER_TRADE_FRACTION=0.05`, `MAX_EXPOSURE_FRACTION=1.0`,
  `MAX_DAILY_LOSS_FRACTION=0.10`, `MAX_SIMULTANEOUS_POSITIONS=50`, `MIN_REWARD_RISK=2.0`,
  `max_candidate_price <= 100`.
- `.env.example` rewritten as the canonical variable reference; README gained a Configuration
  section.
- Tests: `backend/tests/test_config_layers.py` (19).

### PHASE 2 — Remove Synthetic/Demo Production Runtime · DONE

- **Deleted** `backend/market_data/mock_provider.py` and the demo-history generator.
- Added `backend/testing/` (test-only fixtures: `MockMarketDataProvider`, `FixtureHistoryProvider`),
  `backend/market_data/unconfigured.py`, `backend/market_data/registry.py`,
  `backend/scanner/strategy_analyzer.py`, `backend/execution/local_paper.py`.
- `MarketDataService` gained `configured` / `provider_id` / `status`.
- `ApplicationRuntime` and `ConnectionCenter` rewritten: single clock, dependency injection of
  `market_data_provider`, no hard-coded "mock means configured" branches.
- Tests: `test_runtime_connections.py`, `test_market_data_registry.py`.

### PHASE 3 — Real Market-Data Bridge + Paper Position Lifecycle · DONE (162 passed, 16 subtests)

- **Portfolio:** `PriceState` (`FRESH` / `STALE` / `UNPRICED`); `Position` gained `market_price:
  float | None`, `price_as_of`, `price_state`, `price_reason`; `unrealized_pnl` is `None` when
  unpriced; `auto_exit_enabled` requires `FRESH`.
- **Valuation:** `backend/market_data/bridge.py` with availability states `CONNECTED` /
  `NOT_CONFIGURED` / `UNAVAILABLE` / `STALE` / `ERROR`; staleness config-driven
  (`MARKET_DATA_STALE_AFTER_SECONDS`, default 300).
  `backend/paper_trading/valuation.py` is the **only** production path that moves a paper
  position's price.
- **Automatic exits gated on real marks:** `PaperTradingEngine.on_bar`/`on_quote` require `FRESH`
  with `price_as_of` set before SL/TP can fire. Manual close works; `close_position` refuses to
  invent a price.
- **Restart:** `backend/paper_trading/reconcile.py` rebuilds state from the journal; restored
  positions start `UNPRICED`.
- **Wiring:** `ApplicationRuntime` exposes `market_data_bridge`, `valuation`, `reconciliation`,
  `refresh_market()`, a background refresh loop, availability fields in `status()`.
- **API:** `/api/positions` reports `current_price`, `price_state`, `price_as_of`,
  `price_age_seconds`, `price_reason`, `r_multiple` (null when unpriced), `auto_exit_enabled`,
  plus top-level availability/valuation fields.
- **Frontend:** `STALE` badges with live quote age, `UNPRICED` rows render `NOT VALUED` — never a
  fabricated `$0`.
- Tests: `test_market_data_bridge.py` (10), `test_position_valuation.py` (17), 3 existing tests
  adapted to the stricter FRESH-mark contract.

### PHASE 4 — TradingView Integration · DONE (203 passed, 30 subtests)

- **Endpoint:** `POST /api/webhooks/tradingview` (`backend/core/server.py`) — route-first, never
  requires `X-Owner-Token`, hard body limit (413), 500 with a machine-readable body on failure.
- **Auth:** `backend/tradingview/auth.py` — dedicated `TRADINGVIEW_WEBHOOK_SECRET`, constant-time
  HMAC (`X-Webhook-Signature: sha256=<hex>`) or payload `secret` field; 401 `AUTH_FAILED`; the
  secret is never logged, returned, or stored in SQLite.
- **Boundary:** `backend/tradingview/payload.py` — JSON object decode with size limit, TradingView
  alert field aliases, action vocabulary (`ENTER_LONG` / `ENTER_SHORT` / `EXIT` plus aliases), and
  deterministic body-hash / explicit-id event identity.
- **Signal layer:** `backend/signals/` — `NormalizedSignal`, `SignalAction`, shared validation
  (symbol shape and universe membership, price levels, whole-number quantity, timestamps with
  `max_signal_age_seconds` and 60 s future tolerance, strategy allowlist) and
  `to_trade_setup()`.
- **Service:** `backend/tradingview/service.py` — fail-closed order (enabled → body size →
  signature → payload secret → normalize → replay → risk → execution) under one lock;
  `RiskEngine.evaluate()` runs before any execution; an alert quantity only caps the risk-approved
  quantity; entry stays strictly below `max_candidate_price`; exits close the full position at the
  reported price.
- **Idempotency:** `backend/signals/idempotency.py` — `body:<sha256>` plus `id:<explicit>` keys,
  `SIGNAL_PROCESSED` journal hydration on restart, `409 DUPLICATE_SIGNAL` on replay; auth,
  validation, and 5xx outcomes are not remembered so a corrected retry is processed.
- **Responses:** 202 `ACCEPTED`; 400 `INVALID_PAYLOAD` (with `reason`); 401 `AUTH_FAILED`;
  409 `DUPLICATE_SIGNAL`; 413 `PAYLOAD_TOO_LARGE`; 422 `RISK_REJECTED` / `NO_OPEN_POSITION` /
  `QUANTITY_MISMATCH`; 503 `NOT_CONFIGURED` / `EXECUTION_UNAVAILABLE`; 500 `INTERNAL_ERROR`.
- **Connection state:** `ConnectionCenter` `tradingview` section plus a local authentication
  `self_check` that makes no network request — `NOT_CONFIGURED` / `UNAVAILABLE` / `CONNECTED` /
  `STALE` / `ERROR`; a configured secret alone is never `CONNECTED`.
- **Config and docs:** `TradingViewSettings` gained `max_signal_age_seconds`,
  `stale_after_seconds`, `max_body_bytes` validation and `describe()` fields; `.env.example`
  keys; README section; `docs/TRADINGVIEW.md`.
- **Frontend:** `ConnectionsPanel.tsx` TradingView card reports endpoint, role, state and reason,
  signal counters, allowed strategies, and last signal; the card badge follows `ready`.
- **Verification:** `203 passed, 30 subtests`; `tsc --noEmit` clean; `npm run build` succeeds;
  HTTP smoke against a running instance (`/api/status`, `/api/positions`, `/api/readiness`,
  `/api/connections` parsed as JSON; signed alert → 202, identical replay → 409, bad signature →
  401, malformed body → 400, disabled endpoint → 503 `NOT_CONFIGURED`, connection test →
  `PASSED`, position reported `UNPRICED` with `current_price: null`, no secret in any payload,
  no `MOCK` in status); `git diff` reviewed; no synthetic data introduced.
- **Invariants held:** zero fabricated production market data; the alert price is signal/entry
  metadata only and never an ongoing market price (`UNPRICED` without a real feed); TradingView is
  a signal source only — no direct TradingView → broker execution and no bypass of `RiskEngine` or
  the execution abstraction; test-only mocks stay in `backend/testing/` and `backend/tests/`.

---

### PHASE 5 — Real Market-Data Providers · **DONE**
- Polygon (`/v2/last/trade`, `/v2/aggs` paginated, `/v3/reference/tickers`) and Alpha Vantage
  (`TIME_SERIES_INTRADAY`, `TIME_SERIES_DAILY`, `SYNTAX_SEARCH`) adapters behind the existing
  `MarketDataProvider` → registry → `MarketDataService` → `MarketDataBridge` → paper valuation
  path; stdlib `urllib` behind an injectable transport (tests never touch the network).
- Honest provider states driven by recorded evidence — credentials alone report `UNAVAILABLE`,
  `CONNECTED` only after a real successful request, quiet windows downgrade to `STALE`, throttles
  report `RATE_LIMITED` with a window; classified failures (`ProviderAuthenticationError`,
  `ProviderRateLimitError`, `ProviderTimeoutError`, `ProviderConnectionError`,
  `ProviderUnavailableError`, `ProviderResponseError`) never become fabricated data.
- Provider-aware credentials (`POLYGON_API_KEY` / `ALPHAVANTAGE_API_KEY`), catalog
  `implemented=True` for both adapters, `MARKET_DATA_TIMEOUT_SECONDS`, backend-driven
  ConnectionCenter market-data probe, `/api/backtest` `NO_DATA` honesty, symbol lookup,
  Connections panel + backtest panel copy, `docs/MARKET_DATA.md`.
- **Verification:** `262 passed, 66 subtests`; `tsc --noEmit` clean; `npm run build` succeeds;
  HTTP smoke with no provider keys configured (`NOT_CONFIGURED` everywhere, no secret in any
  payload, no network request in the unconfigured path); `git diff` reviewed; no synthetic data
  introduced — provider tests run only against scripted transports in `backend/testing/http.py`.
- **Untouched by this checkpoint:** provider implementations, registry, market-data service, health
  states, error taxonomy, freshness handling, real-data valuation, `NO_DATA` / `UNPRICED`
  behavior, ConnectionCenter market-data checks, frontend provider status UI, tests, and the
  Phase 4 TradingView implementation.

---

## Future phases (none started)

### PHASE 6 — Connection & Execution Provider Framework · **NOT STARTED**

> Documentation-only checkpoint: this scope is recorded, not implemented. Do not begin without
> explicit approval. This phase is **not** an "Alpaca integration phase" — it is the
> broker/provider-agnostic framework; Alpaca and Robinhood are example providers a client may
> connect.

Scope the future phase must eventually cover:

- client-provided credentials/connection details and secure credential handling (secrets never
  exposed, logged, or committed)
- authentication and connectivity verification
- account verification and trading permission verification
- capability detection: supported assets, supported order types, buying power/account
  information, account status, positions
- order submission, order cancellation, order modification where supported, order status,
  fills/executions, fees/commissions where available
- provider-specific limitations surfaced honestly
- paper/live separation and explicit live-trading safety gates (LIVE must never activate
  accidentally; no fake broker account)
- reconciliation of orders/fills against the provider
- provider health checks (credentials alone are not connectivity)
- kill switch / emergency stop
- a **standardized internal execution interface** consumed by the execution router — no
  broker-specific logic in strategies, risk, AI advisory, or position management
- multiple client connections (Connection A → Robinhood, Connection B → Alpaca, Connection C →
  another provider), each independently verified/authorized and selectable by client configuration
- extensibility: adding a provider must not require modifying the core trading strategy/decision
  engine; if automatic provider identification is impossible, the UI asks the client to identify it

### PHASE 7 — Canonical Trading Pipeline
- Establish canonical `TradingPipeline`; normalize/validate signals;
  Strategy → AI advisory → Risk → Portfolio Rules → Execution Router; mode-aware execution; remove
  duplicate orchestration paths; broker/provider agnostic; ensure no component can bypass
  risk/execution controls.

### PHASE 8 — Risk Hardening
- Move all entry/exposure constraints into the proper risk layer; configurable candidate-price and
  position limits; position sizing; daily loss limits; exposure limits; stop-loss/take-profit
  validation; risk re-evaluation before execution; fail closed.

### PHASE 9 — Order Lifecycle Management & Reconciliation
- Canonical order state machine across the pipeline: submitted → accepted → partially filled →
  filled → canceled/rejected/expired; order/fill reconciliation against the execution provider
  and the local journal; drift detection and honest unresolved states; restart recovery.

### PHASE 10 — First-Class Position Management
- Position management as a **first-class subsystem**, not a side effect of order execution:
  entry → open position → monitoring → risk changes → exit conditions → close → reconciliation →
  final P&L; dynamic position management; scaling/adjustment where supported; positions remain
  `UNPRICED` until a fresh real mark exists.

### PHASE 11 — Market-Session / Market-Hours Handling
- Market-session awareness (pre/open/after/close/holiday), trading-window enforcement per asset
  class and per provider capability, no order submission outside allowed sessions, session-aware
  scanning and valuation staleness.

### PHASE 12 — Autonomous Decision Engine
- Continuously scan/analyze real market data, identify valid opportunities, run strategy
  evaluation, apply AI advisory where applicable, and produce trading decisions — every decision
  still passing `RiskEngine` and the standardized execution interface. No direct strategy/AI →
  broker path.

### PHASE 13 — Strategy Evaluation & Anti-Overfitting Validation
- Strategy evaluation framework; anti-overfitting validation (walk-forward, out-of-sample,
  parameter-sensitivity, multiple-testing controls); honest performance attribution; reject
  strategies that only fit history.

### PHASE 14 — Backtesting Product Quality
- Configurable dates/lookback; real historical provider support; consistent
  fees/slippage/sizing/risk model; meaningful statistical reporting; prevent misleading
  tiny-sample headline metrics; no fabricated production results; clear
  incomplete/insufficient-data states.

### PHASE 15 — Connection Center
- Market-data connections, execution-provider connections, TradingView, notification integrations;
  honest `NOT_CONFIGURED` / `CONNECTED` / `ERROR` / `UNAVAILABLE` states; credential presence must
  not equal successful connectivity.

### PHASE 16 — Notifications
- Webhook, Discord, Telegram; stdlib HTTP where appropriate; notification events for relevant
  trading/system events; no fake successful delivery status.

### PHASE 17 — 24/7 Worker / Operations Layer
- Long-running worker/operations layer: supervised background loops (data refresh, scanning,
  decision, execution, reconciliation, journaling), graceful degradation, restart/recovery,
  scheduling, and operational runbooks.

### PHASE 18 — Monitoring / Health
- System, market-data provider, and execution-provider health; readiness/liveness reporting;
  alerting on degradation, stale feeds, failed reconciliations, and kill-switch state; no
  fabricated health.

### PHASE 19 — Reporting / Performance / Unified Clock
- Daily automated system/trading log report; relevant trades; P&L/performance; risk events;
  errors; connection status; unified application clock; accurate timestamps; no hardcoded demo
  dates.

### PHASE 20 — Security / API Hardening
- Owner authentication for state-changing APIs; TradingView webhook uses its dedicated webhook
  authentication (not owner auth); input validation; secret redaction; safe error responses;
  appropriate access control; rate limiting/replay protection where applicable; credential
  storage hardening.

### PHASE 21 — Database / Persistence Hardening
- Proper migrations/versioning; persistent positions/state where required; reconciliation after
  restart; eliminate known in-memory vs persistent divergence; preserve journal integrity.

### PHASE 22 — Dashboard / Frontend
- Professional trading dashboard; real market state, positions, P&L, risk, connections,
  TradingView, charts, backtesting, system health; clear empty/unconfigured/unpriced states;
  hand-built SVG charts where appropriate; no fake/demo numbers.

### PHASE 23 — Testing / Dependencies / CI
- Comprehensive backend tests; frontend Vitest tests; HTTP/API tests; integration tests; provider
  contract tests; execution-provider contract tests; TradingView webhook tests; dependency
  manifest; CI; frontend/backend verification; regression coverage.

### PHASE 24 — Final Integration / Production Readiness
- Complete end-to-end verification; BACKTEST/PAPER/LIVE mode validation; real-data flow validation;
  TradingView → signal → pipeline → paper execution; real market data → position marking → P&L;
  execution provider → order/fill → reconciliation; risk enforcement; security verification;
  frontend/backend integration; remove dead/demo paths; final documentation; final test/build/HTTP
  verification; production-readiness assessment toward the autonomous trading goal.
