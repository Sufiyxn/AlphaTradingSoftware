# Alpha Trading Platform — Binding 17-Phase Roadmap

> **Approved.** Phases 1–4 are complete and verified. Phases 5–17 are binding scope.
> **Do not reorder phases without explicit approval.** Each phase ends only after the verification
> protocol below passes with exact numbers reported.

---

## Binding constraints (apply to every phase)

1. **No fabricated market data, ever, in production.** No synthetic candles, ticks, prices, fills,
   P&L, demo symbols, fake balances, or fake trades. Test fixtures live only in `backend/testing/`
   and are never wired by `ApplicationRuntime`. `MOCK` is not a product mode;
   `backend/config/loader.py` normalizes `MOCK`/`TEST`/`FIXTURE` away for the running app.
2. **One mandatory execution path — `TradingPipeline` is the canonical target architecture.**
   `TradingOrchestrator` is not a rename substitute for it. `TradingOrchestrator` stays functional
   through Phases 4–6 where practical; the proper `TradingPipeline` extraction/consolidation happens
   in **Phase 7**. The eventual canonical flow:

   ```
   Signal Source
       -> Signal Normalizer / Validator
       -> TradingPipeline
            -> Strategy
            -> optional AI analysis/advisory
            -> Risk
            -> Portfolio Rules
            -> Execution Router
       -> Mode-specific execution
   ```

   The frontend, TradingView, AI, and individual providers must never bypass this architecture.
   Until Phase 7 lands, every new signal path must still pass `RiskEngine` before execution.
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
   state only.
7. **HTTP auth split.** `POST /api/webhooks/tradingview` uses its dedicated webhook-secret/HMAC
   authentication and **must not** require the owner token. Every other state-changing `POST`
   requires `X-Owner-Token` with owner controls enabled.
8. **Data-dependent features degrade honestly.** Structured `NOT_CONFIGURED` / `UNAVAILABLE` with a
   reason — never a fabricated value, never a global error banner. Credential presence is not
   connectivity.
9. **Working code is not rewritten.** Extend, wire, and test; do not re-architect passing systems.
10. **Locked integration choices.** Market data = Polygon + Alpha Vantage. Broker = Alpaca.
    Notifiers = Local, Generic Webhook, Discord, Telegram over stdlib HTTP. Charts = hand-rolled
    SVG (no chart library). Frontend tests = `vitest` + `jsdom` + `@testing-library/react`.

### Per-phase verification protocol

- `python -m pytest backend/tests -q` (with `PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`) —
  count must not drop below the previous phase (203 as of Phase 4).
- `node node_modules\typescript\bin\tsc --noEmit -p tsconfig.json` in `frontend/`.
- `npm.cmd run build` in `frontend/`.
- HTTP smoke against a running instance: `/api/status`, `/api/positions`, `/api/readiness`,
  `/api/connections` parsed as JSON.
- `git diff` reviewed for accidental regressions.
- Explicit confirmation: no synthetic/demo data introduced, no `MOCK` reachable from the
  production runtime.
- Frontend tests, once Phase 16 lands.

---

## The roadmap

| # | Phase | Status |
|---|---|---|
| 1 | Configuration / Mode Foundation | **DONE** |
| 2 | Remove Synthetic/Demo Production Runtime | **DONE** |
| 3 | Real Market-Data Bridge + Paper Position Lifecycle | **DONE** |
| 4 | TradingView Integration | **DONE** |
| 5 | Real Market-Data Providers | **NEXT** |
| 6 | Alpaca Broker Integration | pending |
| 7 | TradingPipeline Consolidation | pending |
| 8 | Risk Hardening | pending |
| 9 | Backtesting Product Quality | pending |
| 10 | Connection Center | pending |
| 11 | Notifications | pending |
| 12 | Reporting / Unified Clock | pending |
| 13 | Security / API Hardening | pending |
| 14 | Database / Persistence | pending |
| 15 | Dashboard / Frontend | pending |
| 16 | Testing / Dependencies / CI | pending |
| 17 | Final Integration / Production Readiness | pending |

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

### PHASE 5 — Real Market-Data Providers · **NEXT**
- Polygon, Alpha Vantage, provider registry, real historical/current data, normalized provider
  interface, honest connection states, quote freshness/staleness, provider errors/retries where
  appropriate, no synthetic production data.

### PHASE 6 — Alpaca Broker Integration
- Alpaca adapter, sandbox/paper/live environments where supported, credential/configuration handling,
  order mapping, order/fill/status reconciliation, explicit live-trading safety gates, LIVE must
  never activate accidentally, no fake broker account.

### PHASE 7 — TradingPipeline Consolidation
- Establish canonical `TradingPipeline`; normalize/validate signals;
  Strategy → AI advisory → Risk → Portfolio Rules → Execution Router; mode-aware execution; remove
  duplicate orchestration paths; broker/provider agnostic; ensure no component can bypass
  risk/execution controls.

### PHASE 8 — Risk Hardening
- Move all entry/exposure constraints into the proper risk layer; configurable candidate-price and
  position limits; position sizing; daily loss limits; exposure limits; stop-loss/take-profit
  validation; risk re-evaluation before execution; fail closed.

### PHASE 9 — Backtesting Product Quality
- Configurable dates/lookback; real historical provider support; consistent
  fees/slippage/sizing/risk model; meaningful statistical reporting; prevent misleading
  tiny-sample headline metrics; no fabricated production results; clear
  incomplete/insufficient-data states.

### PHASE 10 — Connection Center
- Market-data connections, broker connections, TradingView, notification integrations; honest
  `NOT_CONFIGURED` / `CONNECTED` / `ERROR` / `UNAVAILABLE` states; credential presence must not
  equal successful connectivity.

### PHASE 11 — Notifications
- Webhook, Discord, Telegram; stdlib HTTP where appropriate; notification events for relevant
  trading/system events; no fake successful delivery status.

### PHASE 12 — Reporting / Unified Clock
- Daily automated system/trading log report; relevant trades; P&L/performance; risk events;
  errors; connection status; unified application clock; accurate timestamps; no hardcoded demo
  dates.

### PHASE 13 — Security / API Hardening
- Owner authentication for state-changing APIs; TradingView webhook uses its dedicated webhook
  authentication (not owner auth); input validation; secret redaction; safe error responses;
  appropriate access control; rate limiting/replay protection where applicable.

### PHASE 14 — Database / Persistence
- Proper migrations/versioning; persistent positions/state where required; reconciliation after
  restart; eliminate known in-memory vs persistent divergence; preserve journal integrity.

### PHASE 15 — Dashboard / Frontend
- Professional trading dashboard; real market state, positions, P&L, risk, connections,
  TradingView, charts, backtesting, system health; clear empty/unconfigured/unpriced states;
  hand-built SVG charts where appropriate; no fake/demo numbers.

### PHASE 16 — Testing / Dependencies / CI
- Comprehensive backend tests; frontend Vitest tests; HTTP/API tests; integration tests; provider
  contract tests; broker contract tests; TradingView webhook tests; dependency manifest; CI;
  frontend/backend verification; regression coverage.

### PHASE 17 — Final Integration / Production Readiness
- Complete end-to-end verification; BACKTEST/PAPER/LIVE mode validation; real-data flow validation;
  TradingView → signal → pipeline → paper execution; real market data → position marking → P&L;
  broker → order/fill → reconciliation; risk enforcement; security verification;
  frontend/backend integration; remove dead/demo paths; final documentation; final test/build/HTTP
  verification; production-readiness assessment.
