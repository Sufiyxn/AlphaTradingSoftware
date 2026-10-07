# Alpha Trading Platform — Binding 17-Phase Roadmap

> **Approved.** Phases 1–3 are complete and verified. Phases 4–17 are binding scope.
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
  count must not drop below the previous phase (162 as of Phase 3).
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
| 4 | TradingView Integration | **NEXT** |
| 5 | Real Market-Data Providers | pending |
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

---

## PHASE 4 — TradingView Integration · **NEXT**

- TradingView webhook ingestion
- dedicated webhook secret/HMAC validation
- signal validation and normalization
- idempotency/replay protection
- clear accepted/rejected responses
- TradingView connection/configuration state
- integrate signals into the existing paper workflow
- **no direct TradingView → broker execution**
- **no fabricated pricing**

**Phase 4 invariants (must hold on completion):** zero fabricated production market data; zero fake
demo accounts, balances, trades or prices; `UNPRICED` when continuous real pricing is unavailable;
TradingView is a signal source, not a broker and not a price source; no automatic SL/TP without a
valid current real price; test-only mocks remain quarantined to `backend/testing/` and
`backend/tests/`.

### PHASE 5 — Real Market-Data Providers
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
