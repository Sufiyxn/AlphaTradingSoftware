# Alpha Trading Platform — Binding 24-Phase Roadmap

> **Approved.** Phases 1–9 are complete and verified. Phases 6 and 7 are **committed at their
> checkpoints** (`Complete phase 6 execution provider framework`, `2f1eee5`), Phase 8 is committed
> as `c8420ab`, and Phase 9 — *Order Lifecycle Management & Reconciliation* — is committed as
> `Complete phase 9 order lifecycle and reconciliation`. Phases 10–24 are
> binding scope.
> **Do not start, reorder, or collapse phases without explicit approval.** Each phase ends only
> after the verification protocol below passes with exact numbers reported.
>
> *Documentation-only checkpoint update (post-Phase 5): Phase 5 is recorded as DONE, Phase 6 is
> renamed/reframed as the provider-agnostic "Connection & Execution Provider Framework" and stayed
> NOT STARTED, and the pending phases were renumbered/expanded to cover the autonomous end goal.
> No Phase 5 or TradingView implementation code was changed by that update.*
>
> *Phase 6 checkpoint: the provider-agnostic connection/execution framework described below is
> implemented and verified (314 passed, 102 subtests) and committed as
> `Complete phase 6 execution provider framework`. It ships one implemented provider — the
> in-process `local_paper` account — plus honestly advertised, deliberately unimplemented external
> providers (`alpaca`, `robinhood`).*
>
> *Phase 7 was committed as `2f1eee5 Complete phase 7 canonical trading pipeline` (16 files,
> +1613/−109) on top of Phase 6; nothing before it was amended.*
> *Phase 8 was committed as `c8420ab Complete phase 8 risk hardening` on top of Phase 7;
> nothing before it was amended. The risk hardening described below is implemented and verified
> (395 passed, 150 subtests; `tsc --noEmit` clean; `npm run build` succeeds; HTTP smoke passed
> with 46 checks — accepted trade, reward:risk < 2, wrong-side/zero/missing price, duplicate
> symbol, stale timestamp, tightened exposure, and kill-switch rejections, each journaled in
> `risk_decisions` with its deciding code while exactly 1 order row reached the paper adapter).
> `RiskEngine` is the single deterministic pre-trade boundary: sizing, exposure, daily loss,
> duplicate-symbol, price sanity, 2:1 reward:risk, kill switch, and configuration limits are
> enforced before any adapter can be called, and `RiskDecision` now records every field needed
> to audit the decision from the journal alone. Full detail: `docs/PIPELINE.md`.*
> *Phase 9 (Order Lifecycle & Reconciliation) was approved and is implemented as described
> below: `backend/orders/` owns the canonical order — a platform-minted `client_order_id`, a
> `provider_order_id` taken verbatim, one state machine over submitted → accepted → partially
> filled → filled plus terminal cancel/reject/expire, and executed quantity always derived from
> the recorded fills; open orders reserve risk capacity under one submission lock; the
> reconciler is read-only and only reports `MATCH` for a complete, error-free snapshot; every
> cancellation attempt has its own outcome; orders/fills/lifecycle events are journaled and
> rebuilt after a restart; `GET /api/orders` and owner-guarded `POST /api/orders/cancel`,
> `/api/orders/reconcile` expose it. Verification: `467 passed, 150 subtests`. Full detail:
> `docs/ORDERS.md`.*
> **Phase 10 has started: checkpoint 1 (durable provider-order identity) is implemented and
> verified — `486 passed, 150 subtests`; checkpoint 2 (durable fill identity + a durable fill
> journal) is implemented and verified — `509 passed, 150 subtests`; checkpoint 3 (positions are
> booked from durable fills, exactly once) is implemented and verified — `523 passed, 150
> subtests`; `tsc --noEmit` clean; `npm run build` succeeds; HTTP smoke passed with 41 checks and
> is now committed as `scripts/http_smoke.py` (the four documented endpoints parsed as JSON, a
> signed paper trade whose order, fill, position, risk decision and activity rows are all served
> over HTTP, replay and missing-authentication refusals, owner-token guards, read-only reconcile,
> the durable fill journal exported, and no credential in any response body) — checkpoints 4–6
> have not started.**

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
   Phase 7 delivered this as `backend/pipeline/` (`TradingPipeline`): every signal path —
   TradingView, the orchestrator, and the paper engine — enters through
   `PaperTradingEngine.submit_decision` with an `ExecutionIntent` (or a pre-approved
   `RiskDecision`), and no production file outside `backend/execution/` and `backend/pipeline/`
   may call `place_order` — enforced by `backend/tests/test_pipeline.py`. Every signal path must
   still pass `RiskEngine` before execution — and that requirement never expires.
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
  `/api/connections` parsed as JSON — committed as `scripts/http_smoke.py`, which boots the app
  in-process on a temporary database with no market-data provider attached (so it can never
  reach a vendor API) and checks the whole documented HTTP surface, including a signed webhook
  trade, the owner-token guards, and a scan of every response for the credentials that run
  configured.
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
| **Execution providers** | Robinhood, Alpaca, future brokers/trading platforms | Submit/cancel/modify orders and report fills **through a standardized internal execution interface**, only after `RiskEngine`. Framework in place (Phase 6); no external provider adapter is installed yet. |

Forbidden couplings — architectural, permanent:

- TradingView → broker
- AI → broker
- Strategy → broker
- Market data provider → broker

`RiskEngine` remains between decision generation and execution in every path.

### Execution is provider-agnostic (Phase 6 shape — framework implemented, external adapters not installed)

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
| 6 | Connection & Execution Provider Framework | **DONE** |
| 7 | Canonical Trading Pipeline | **DONE** (committed: `2f1eee5`) |
| 8 | Risk Hardening | **DONE** (committed: `c8420ab`) |
| 9 | Order Lifecycle Management & Reconciliation | **DONE** (committed: `Complete phase 9 order lifecycle and reconciliation`) |
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

> The completed sequence 1–9 is fixed (Phase 6 is committed at its checkpoint; Phase 7 is
> committed at `2f1eee5`; Phase 8 is committed at `c8420ab`; Phase 9 was explicitly approved
> and is committed at its own checkpoint). Phases 10–24
> are all **pending**; their internal order may be refined only by an explicit, approved
> documentation update. **Phase 10 must not begin without explicit approval.**

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

## Phase 6–9 (committed), and future phases (not started)

### PHASE 6 — Connection & Execution Provider Framework · **DONE (COMMITTED)**

> Implemented and verified (314 passed, 102 subtests), committed as
> `Complete phase 6 execution provider framework`. This phase is **not** an "Alpaca integration
> phase" — it is the broker/provider-agnostic framework; Alpaca and Robinhood are example
> providers a client may connect, and both are advertised here as *not installed*.

Delivered scope (was the future-phase scope below):

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

### PHASE 7 — Canonical Trading Pipeline · **DONE (COMMITTED)**

> Implemented and verified (327 passed, 121 subtests; `tsc --noEmit` clean; `npm run build`
> succeeds; HTTP smoke passed) and committed as
> `2f1eee5 Complete phase 7 canonical trading pipeline` (16 files, +1613/−109). Full detail:
> `docs/PIPELINE.md`.

Delivered scope (was the future-phase scope below):

- canonical `TradingPipeline` (`backend/pipeline/`): validate → signal freshness → resolve and
  verify the connection through `ConnectionManager` → `RiskEngine` (with the signal's quantity
  cap, never widened) → `ExecutionSafetyGate` → execution mode + capability checks → submit
  through the verified adapter → normalize the provider status → journal → one normalized
  `PipelineResult`
- one deterministic outcome code per request from `PipelineCode` (16 codes, all exercised by
  `backend/tests/test_pipeline.py`): `ACCEPTED`, `INVALID_REQUEST`, `STALE_SIGNAL`,
  `DUPLICATE_INTENT`, `RISK_REJECTED`, `KILL_SWITCH`, `SAFETY_REJECTED`, `CONNECTION_NOT_FOUND`,
  `CONNECTION_NOT_READY`, `CAPABILITY_UNAVAILABLE`, `UNSUPPORTED_MODE`, `PROVIDER_UNAVAILABLE`,
  `PROVIDER_AUTH_FAILURE`, `PROVIDER_TIMEOUT`, `PROVIDER_RATE_LIMIT`,
  `PROVIDER_EXECUTION_REJECTION`
- idempotency: at most one order per key in a bounded window; a duplicate never submits and
  never books twice
- all three signal paths enter through `PaperTradingEngine.submit_decision` with an
  `ExecutionIntent` (TradingView with freshness + quantity cap + `signal_id`, the orchestrator
  with scanner setups, paper tests with pre-approved `RiskDecision`s) — risk is evaluated exactly
  once, inside the pipeline, and journaled exactly once
- the runtime shares one `ConnectionManager` between the Connection Center and order routing;
  swapping a connection's adapter invalidates its verification evidence and the pipeline
  re-verifies before any order (`ConnectionManager.set_adapter`)
- no-bypass enforcement: no production file outside `backend/execution/` and `backend/pipeline/`
  calls `place_order`, and neither signal service calls `RiskEngine.evaluate` directly
- original scope, as approved: establish canonical `TradingPipeline`; normalize/validate signals;
  Strategy → AI advisory → Risk → Portfolio Rules → Execution Router; mode-aware execution; remove
  duplicate orchestration paths; broker/provider agnostic; ensure no component can bypass
  risk/execution controls.

### PHASE 8 — Risk Hardening · **DONE (COMMITTED)**

> Implemented and verified (395 passed, 150 subtests — `backend/tests/test_risk_hardening.py`
> adds 68 tests / 29 subtests on top of the Phase 1–7 suites; `tsc --noEmit` clean;
> `npm run build` succeeds; HTTP smoke passed with 46 checks) and committed as
> `c8420ab Complete phase 8 risk hardening`. Full detail: `docs/PIPELINE.md` ("Risk controls").

Delivered scope (was the future-phase scope below):

- all entry/exposure constraints live in the risk layer as one deterministic 7-step evaluation
  (`backend/risk/service.py`): configuration + kill switch → price sanity + entry-price ceiling →
  direction/geometry (long: stop < entry < target; short: target < entry < stop) → trade risk > 0
  and reward:risk ≥ 2.0 → portfolio state (valuation freshness, equity, daily loss, max
  positions, duplicate symbol) → sizing with `approved = min(safe, requested)` (a request is only
  ever reduced) → final invariant checks (trade risk, max quantity, notional, symbol exposure,
  projected portfolio exposure)
- configurable candidate-price and position limits: `RISK_MAX_QUANTITY` (10000, ceiling 100000),
  `RISK_MAX_SYMBOL_EXPOSURE` (25000), `RISK_MAX_ENTRY_PRICE` (100; may only tighten
  `MAX_CANDIDATE_PRICE`, never widen it), `RISK_REQUIRE_FRESH_VALUATION` (false), plus the
  existing `RISK_FRACTION`, `RISK_MAX_PER_TRADE`, `RISK_MAX_RISK_AMOUNT`,
  `RISK_MAX_POSITION_NOTIONAL`, `RISK_MAX_PORTFOLIO_EXPOSURE`, `RISK_MAX_POSITIONS`,
  `RISK_MAX_DAILY_LOSS_FRACTION`, `RISK_MIN_REWARD_RISK` (floor 2.0), and
  `RISK_ALLOW_DUPLICATE_SYMBOL` — every floor/ceiling is un-loosenable; a loosening value fails
  at startup
- position sizing as a documented constraint solver (`backend/risk/sizing.py`):
  `risk_budget = min(available_capital × risk_fraction, equity × max_risk_per_trade,
  max_risk_amount)`; `risk_quantity = risk_budget // |entry − stop|`; exposure/cash/notional/
  symbol/max-quantity terms; safe quantity is their minimum; zero safe quantity rejects
- daily loss limit, exposure limits, and duplicate-symbol fail closed with machine-readable
  binding-limit reasons; the daily-loss window rolls on the UTC calendar day and a close landing
  on a new day now starts that day's window (`Portfolio.close_position` rolls before
  accumulating), with restart persistence rebuilt from the journal by `reconcile_journal`
- stop-loss/take-profit (direction, side, and 2:1 minimum) validated before execution; risk is
  evaluated exactly once per intent inside `TradingPipeline` and never re-evaluated or weakened
  for a pre-approved decision; no execution without an approved risk decision
- auditability: `RiskDecision` records `code` (first failing rule), requested vs approved
  quantity, entry/stop/target, risk/reward per share, reward:risk, current/projected exposure,
  daily P&L, equity, symbol-open state, active limits, and `decided_at`; the pipeline journals
  the deciding code and quantities into `risk_decisions` and `system_events`
- fail closed throughout: missing/NaN/infinite prices are rejected, never invented; unpriced
  positions contribute zero market value to equity; `backend/risk/` stays provider-agnostic
  (enforced by a source scan)
- original scope, as approved: move all entry/exposure constraints into the proper risk layer;
  configurable candidate-price and position limits; position sizing; daily loss limits; exposure
  limits; stop-loss/take-profit validation; risk re-evaluation before execution; fail closed.

### PHASE 9 — Order Lifecycle Management & Reconciliation · **DONE (COMMITTED)**

> Implemented and verified (`467 passed, 150 subtests` — `backend/tests/test_order_lifecycle.py`
> adds 71 tests on top of the Phase 1–8 suites; `tsc --noEmit` clean; `npm run build` succeeds;
> HTTP smoke passed; no secret in any response) and committed as
> `Complete phase 9 order lifecycle and reconciliation`. Full detail: `docs/ORDERS.md`.

Delivered scope (as approved):

- **canonical order** — new `backend/orders/` package: `OrderIntent` (the request),
  `Order` (identity + status + fills), `Fill`, and one state machine
  (`VALID_TRANSITIONS` / `can_transition` / `with_status`) over
  `NEW → SUBMITTED → ACCEPTED → PARTIALLY_FILLED → FILLED` plus terminal
  `CANCELLED` / `REJECTED` / `EXPIRED` / `FAILED`; an illegal move raises
  `InvalidOrderTransition` instead of jumping status, re-applying a status is idempotent,
  `UNKNOWN` recovers to a known state but no terminal state ever becomes `UNKNOWN`
- **identity** — `client_order_id` is a platform-minted `uuid4().hex` (never a symbol, never a
  timestamp); `provider_order_id` is stored verbatim from the provider, stays `None` until the
  provider answers, and a later answer without one never clears it; a provider's "new" word
  still normalizes to `ACCEPTED`, never the platform-only `NEW`
- **fills** — `filled_quantity` / `average_fill_price` / `remaining_quantity` are always
  recomputed from the recorded fills (volume-weighted average), de-duplicated by
  `provider_fill_id`; a provider that claims more executed quantity than fills has that gap
  journaled (`RECONCILIATION_DISCREPANCY`) and **never** a fabricated fill; a late fill on a
  terminal order is recorded and journaled without rewriting the status
- **pending-order risk reservation** — `PendingOrderReservation` on
  `PortfolioSnapshot`; sizing subtracts reserved notional from exposure/cash/symbol room, max
  positions counts pending symbols, a pending symbol counts as open (`DUPLICATE_SYMBOL`), the
  exposure invariant uses `exposure + pending_notional + notional`, and
  `RiskDecision.pending_exposure` records what was reserved. The whole risk → adapter →
  registration path runs under one re-entrant `submission_lock` that the paper engine re-enters
  while booking, so two intents can never size against the same capacity
- **pipeline integration** — `TradingPipeline` creates the order only after the provider
  answers (a failed submission records **no** order, only `ORDER_SUBMISSION_FAILED` with the
  intent), journals `ORDER_CREATED`/`ORDER_SUBMITTED`/`ORDER_STATUS_CHANGED`/`ORDER_*_FILL`, and
  returns the canonical order on `PipelineResult.order`; an accepted-but-unfilled answer is the
  new `PipelineCode.SUBMITTED`, not a rejection
- **reconciliation** — `OrderReconciler` is read-only and trust-carrying: `PROVIDER_ERROR` /
  `INCOMPLETE` report empty findings and are never `MATCH`; 13 `DiscrepancyKind` values cover
  missing/unknown/duplicate orders, status/quantity/partial-fill drift, missing or duplicate
  provider fills, terminal-vs-open, and position drift (compared only when the provider
  actually returns a book)
- **cancellation** — `CancellationOutcome` (`CANCELLED`, `ALREADY_CANCELLED`, `NOT_CANCELABLE`,
  `REJECTED_BY_PROVIDER`, `PROVIDER_ERROR`, `ORDER_NOT_FOUND`); a provider failure never
  changes local state, an already-cancelled order is idempotent, a `NEW` order closes locally
  without a provider call, and each attempt journals `ORDER_CANCELLED` /
  `ORDER_CANCELLATION_FAILED`
- **persistence + restart** — `orders` rows (serialized order), a new `order_fills` table with
  `INSERT OR IGNORE` on the fill's own identity, `ORDER_*` / `RECONCILIATION_*` events, and
  `OrderLifecycleService.restore()` rebuilding open orders (and their reservations) from the
  journal, automatically at construction and when a host attaches a journal later. *(Phase 10
  checkpoint 2 later moved durable fills to `order_fill_journal`, keyed by the provider scope as
  well as the fill identity; `order_fills` itself is untouched.)*
- **exposure** — `ApplicationRuntime.list_orders` / `cancel_order` / `reconcile_orders`,
  `GET /api/orders`, owner-guarded `POST /api/orders/cancel` (200/400/404/409) and
  `POST /api/orders/reconcile` (200, read-only)
- original scope, as approved: canonical order state machine across the pipeline;
  order/fill reconciliation against the execution provider and the local journal; drift
  detection and honest unresolved states; restart recovery.

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
