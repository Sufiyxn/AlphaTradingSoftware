# Canonical Trading Pipeline & Risk Controls (Phases 7–8)

The one path from an execution request to an order. Every signal source — a TradingView alert,
the orchestrator's scanner cycle, an internal caller — hands an `ExecutionIntent` (or a
pre-approved `RiskDecision`) to `PaperTradingEngine.submit_decision`, which runs it through
`TradingPipeline`. Nothing else in the product calls `place_order`, and nothing reaches an
adapter without an approved `RiskDecision` from the single `RiskEngine` inside the pipeline.

- Code: `backend/pipeline/` (`models.py`, `service.py`); risk: `backend/risk/`
  (`config.py`, `models.py`, `service.py`, `sizing.py`)
- Entry point: `backend/paper_trading/engine.py` (`submit` / `execute` / `submit_decision`)
- Signal paths: `backend/tradingview/service.py::_execute_entry`,
  `backend/orchestrator/service.py::_run_cycle`
- Wiring: `backend/core/runtime.py` — the paper engine shares the runtime's
  `ConnectionManager`, so the Connection Center reports the same connection the orders use;
  `RiskEngine` is constructed with the loaded `RiskConfig` and the shared clock
- Tests / proof: `backend/tests/test_pipeline.py`, `backend/tests/test_risk_hardening.py`

## Canonical flow

```
signal source (TradingView alert | scanner cycle | internal)
  -> ExecutionIntent (setup + source + freshness + quantity cap + connection + idempotency key)
  -> PaperTradingEngine.submit_decision
  -> TradingPipeline
       1. idempotency lookup      - a completed key returns the first result, DUPLICATE_INTENT
       2. validate input          - INVALID_REQUEST / UNSUPPORTED_MODE
       3. signal freshness        - STALE_SIGNAL (before risk, before any adapter)
       4. resolve + verify        - ConnectionManager.get/verify; CONNECTION_NOT_FOUND /
                                    CONNECTION_NOT_READY / PROVIDER_* by honest state
       5. risk evaluation         - RiskEngine against the live portfolio snapshot, with the
                                    signal's quantity cap passed in as requested_quantity
                                    (reduce-only, folded into sizing); journaled exactly once
                                    -> RISK_REJECTED / KILL_SWITCH
       6. execution safety gate   - ExecutionSafetyGate.authorize(mode)
                                    -> KILL_SWITCH / SAFETY_REJECTED
       7. mode + capabilities     - UNSUPPORTED_MODE / CAPABILITY_UNAVAILABLE
       8. submit                  - connections.adapter_for(id).place_order(...) - the only
                                    place_order call in the product outside backend/execution/
                                    classified failures -> PROVIDER_AUTH_FAILURE /
                                    PROVIDER_TIMEOUT / PROVIDER_RATE_LIMIT / PROVIDER_UNAVAILABLE
       9. normalize status        - fill states -> ACCEPTED, anything else ->
                                    PROVIDER_EXECUTION_REJECTION
      10. journal                 - record_risk_decision, record_order, EXECUTION_SUBMITTED /
                                    EXECUTION_REJECTED / EXECUTION_DUPLICATE (journal errors
                                    never block trading)
      11. PipelineResult          - code, reason, risk, order, setup, connection_id, provider_id,
                                    mode, source, safety evidence
  -> PaperTradingEngine books the fill exactly once -> SubmissionResult
```

Fail-closed ordering matters: invalid input and stale signals are refused **before** risk is
evaluated, an unresolvable connection is refused before risk, risk runs before the gate, and the
adapter is touched last. A gate block does not hide the risk decision — risk is still evaluated
and journaled so the record shows what risk said.

## Risk controls (Phase 8)

`RiskEngine.evaluate(setup, portfolio, *, requested_quantity=None)` is the only place an
intent is judged. It runs seven steps in a fixed order and returns one `RiskDecision`; the
first failing rule becomes `decision.code`, and every rule that failed appends its reason.

1. **Configuration + kill switch** — invalid config or `set_kill_switch(True)` rejects
   (`KILL_SWITCH`); the switch is never overridden anywhere else.
2. **Price sanity** — entry/stop/target must be positive, finite, non-NaN; entry must not
   exceed the entry-price ceiling.
3. **Geometry** — long requires stop < entry < target, short requires target < entry < stop.
4. **Trade risk + reward:risk** — `trade_risk = |entry − stop|`, `trade_reward = |target − entry|`
   (direction-adjusted), both must be > 0, and `reward:risk = trade_reward / trade_risk` must be
   ≥ `min_reward_risk` (floor 2.0, never loosened).
5. **Portfolio state** — valuation freshness (optional strict mode), equity available, daily
   loss limit (`daily_pnl ≤ −max_daily_loss_fraction × current equity`, UTC-day window), max
   simultaneous positions, duplicate-symbol prohibition.
6. **Sizing** — see formulas below; the requested quantity is applied as `approved = min(safe,
   requested)`; a request can only ever *reduce* the size, never widen it.
7. **Final invariants** — per-share risk ≤ limits, quantity ≤ max quantity, notional ≤ position
   notional, symbol exposure and projected portfolio exposure within their caps. When a limit
   has zero remaining room the quantity comes back 0 (`ZERO_SAFE_QUANTITY`) and the binding
   limit's own reason is appended after the generic one.

### Formulas

```
risk_budget     = min(available_capital × risk_fraction,
                      equity × max_risk_per_trade,
                      max_risk_amount)          # last term only when configured
safe_quantity  = min(
                   risk_budget // |entry − stop|,          # trade-risk term
                   max_quantity,                            # share ceiling (10000, unloosenable)
                   max_position_notional // entry,          # per-position notional
                   (max_portfolio_exposure × equity − current_exposure) // entry,
                   (max_symbol_exposure − symbol_notional) // entry,
                   available_capital // entry)              # cash term
approved        = min(safe_quantity, requested_quantity)    # requested only ever reduces
```

Every floor/ceiling is validated at load time: a value that loosens a floor (or widens a
ceiling) raises `ValueError` at startup, so configuration can only tighten risk.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `RISK_ENABLED` | `true` | Master switch; false rejects everything at step 1. |
| `RISK_MIN_REWARD_RISK` | `2.0` | Minimum reward:risk; values below 2.0 fail at load. |
| `RISK_FRACTION` | `0.01` | Fraction of available capital risked per trade (≤ 0.05). |
| `RISK_MAX_PER_TRADE` | `0.01` | Fraction of equity risked per trade (≤ 0.05). |
| `RISK_MAX_RISK_AMOUNT` | unset | Optional absolute currency risk cap per trade; applied only when set. |
| `RISK_MAX_POSITION_NOTIONAL` | `25000` | Gross notional cap for one open position. |
| `RISK_MAX_PORTFOLIO_EXPOSURE` | `0.50` | Fraction of equity that may be in open positions. |
| `RISK_MAX_POSITIONS` | `5` | Maximum simultaneous open positions (ceiling 50). |
| `RISK_MAX_DAILY_LOSS_FRACTION` | `0.03` | Daily loss stop as a fraction of current equity (≤ 0.10). |
| `RISK_MAX_QUANTITY` | `10000` | Absolute share ceiling for one order (ceiling 100000). |
| `RISK_MAX_SYMBOL_EXPOSURE` | `25000` | Gross notional for one symbol, existing position included. |
| `RISK_MAX_ENTRY_PRICE` | `100` | Entry-price ceiling in the risk layer; effective value is min(this, `MAX_CANDIDATE_PRICE`). |
| `RISK_REQUIRE_FRESH_VALUATION` | `false` | When true, any UNPRICED/STALE open position blocks new entries. |
| `RISK_ALLOW_DUPLICATE_SYMBOL` | `false` | Second position in the same symbol (still exposure-capped). |

### Decision audit

`RiskDecision` carries everything needed to replay the reasoning from the journal alone:
`approved` (with `status`, `code` = first failing rule, `reasons`), `requested_quantity` vs
`quantity`, `entry/stop/target`, `risk_amount` / `risk_per_share` / `reward_per_share` /
`reward_risk`, `current_exposure` / `projected_exposure`, `daily_pnl`, `equity`,
`symbol_open`, `limits` (the active `RiskConfig.limits` snapshot), `decided_at`. The pipeline
journals it once via `record_risk_decision` (with its own context: source, pipeline code,
requested/approved quantities) and never re-evaluates or weakens a pre-approved decision.

### Fail-closed honesty

- Missing, NaN, or infinite prices are **rejected**, never invented or defaulted.
- Unpriced positions contribute **zero** market value to equity (they never fabricate value);
  in strict mode they also block new entries.
- `backend/risk/` is provider-agnostic by construction — no broker/exchange/adaptor imports
  (enforced by a source scan test).
- Daily loss state survives restart because `reconcile_journal` rebuilds `daily_pnl` from
  closed trades whose exit date equals the current **UTC** calendar day; the day rolls before a
  close is accumulated (`Portfolio.close_position`), so a close landing on a new day starts that
  day's window rather than poisoning it. Everything else in memory (sizing caches, idempotency
  window) is rebuilt or intentionally volatile.

## Rejection codes

`PipelineCode` (`backend/pipeline/models.py`) is the stable machine vocabulary; `ALL_CODES`
lists all 16 and a test asserts the set never drifts silently.

| Code | Meaning |
|---|---|
| `ACCEPTED` | The provider filled the order (or partially filled with `filled_quantity > 0`). |
| `INVALID_REQUEST` | No/invalid setup, bad price or quantity, freshness without a timestamp. |
| `STALE_SIGNAL` | The signal is older than `max_age_seconds`. |
| `DUPLICATE_INTENT` | This idempotency key already produced a result; nothing new was submitted. |
| `RISK_REJECTED` | `RiskEngine` rejected the setup (or the decision was not approved). |
| `KILL_SWITCH` | The risk emergency stop or the gate's manual stop blocked execution. |
| `SAFETY_REJECTED` | The gate denied the execution mode (e.g. LIVE in this build). |
| `CONNECTION_NOT_FOUND` | The requested connection id does not exist. |
| `CONNECTION_NOT_READY` | Verification did not produce ready evidence (credentials, state, mode lock). |
| `CAPABILITY_UNAVAILABLE` | The provider does not support the needed order type / direction / mode. |
| `UNSUPPORTED_MODE` | The requested mode is not an execution mode, or the connection executes another mode. |
| `PROVIDER_UNAVAILABLE` | The adapter failed (connection/provider error), redacted against credentials. |
| `PROVIDER_AUTH_FAILURE` | The adapter rejected credentials at submission. |
| `PROVIDER_TIMEOUT` | The adapter timed out at submission. |
| `PROVIDER_RATE_LIMIT` | The adapter throttled the request. |
| `PROVIDER_EXECUTION_REJECTION` | The provider answered but did not fill the order. |

## How each signal path maps the result

**TradingView** (`_execute_entry`) preserves its HTTP contract and maps codes:

| Pipeline outcome | HTTP |
|---|---|
| `ACCEPTED` (trade booked) | `202 ACCEPTED` |
| `STALE_SIGNAL` | `400 INVALID_PAYLOAD` / `STALE_TIMESTAMP` |
| `INVALID_REQUEST` | `400 INVALID_PAYLOAD` |
| risk not approved (`RISK_REJECTED`, `KILL_SWITCH`) | `422 RISK_REJECTED` with `reasons` |
| anything else / no trade | `503 EXECUTION_UNAVAILABLE` with `order_status`/`rejection_reason` when an order exists |
| unexpected exception | `500 INTERNAL_ERROR` |

Replay/duplicate protection (`409 DUPLICATE_SIGNAL`) still happens in the webhook layer before
the pipeline; the pipeline's own idempotency key (`signal_id`) is the second line of defence.

**Orchestrator**: reads the same result — approved trade booked → counts as approved;
risk not approved → `_risk_rejected`; order present but not filled → "did not fill" error;
other rejection → recorded with the pipeline code. The orchestrator no longer calls
`RiskEngine.evaluate` or journals risk itself; the pipeline does, and the fixture event order
(`strategy → risk → execution`) still holds.

## Idempotency

`ExecutionIntent.idempotency_key` makes retries safe: the pipeline remembers completed results
(with an order) in a bounded window (`MAX_IDEMPOTENCY_KEYS = 512`, oldest evicted), returns
`DUPLICATE_INTENT` with `duplicate=True` for a repeat, journals `EXECUTION_DUPLICATE`, and the
engine never books a duplicate twice — the first booking stands.

## Connections and adapter swaps

Adapters are reached only through `ConnectionManager.adapter_for(connection_id)` after
`verify(connection_id)` reports ready evidence. `ConnectionManager.set_adapter` swaps the adapter
behind a connection and **invalidates** the old verification evidence
(`ConnectionRecord.invalidate`), so the pipeline re-verifies the new instance before any order.
Standalone engines (constructed without a shared manager) build a private
`ConnectionManager` around their own adapter with a gate wired to the same risk kill switch —
there is no direct-to-adapter path even in tests.

## What the pipeline deliberately does not do

- It does not re-implement risk rules; it asks `RiskEngine` and only ever reduces a decision's
  quantity (never widens it).
- It does not fabricate prices: paper slippage pricing is applied by the engine when it builds
  the intent; the alert price is never a fill price.
- It does not bypass the gate, the connection state, or capability limits, and it never returns
  credentials in a result.
- Strategy selection, AI advisory, and scanner logic stay outside: they produce the setup; the
  pipeline executes it.

## Tests

`backend/tests/test_pipeline.py`:

- the full production chain (shared `ConnectionManager` → `RiskEngine` → `ExecutionSafetyGate`
  → verified adapter → normalized result, journaled, booked exactly once);
- every rejection code exercised, including "risk is still evaluated when the gate blocks" and
  "stale/invalid never reach risk";
- idempotency (one key = one order = one booking; bounded window);
- source scans: `place_order(` appears only under `backend/execution/` and `backend/pipeline/`
  in production code, and both signal services enter through `submit_decision`.

`backend/tests/test_risk_hardening.py` (Phase 8, 68 tests / 29 subtests):

- sizing and every formula term, per-trade risk, 2:1 reward:risk (below-minimum rejection and
  the exact reason string), price sanity (wrong-side stop/target, zero/NaN/infinite prices,
  entry-price ceiling);
- exposure limits, duplicate symbol (both prohibition and opt-in with exposure caps), daily
  loss limit including the UTC day-roll regression and restart persistence through
  `reconcile_journal`, max positions, kill switch end-to-end through `PaperTradingEngine`,
  valuation-freshness strict mode;
- config/env validation (every floor/ceiling fails at load when loosened) and `Config.describe`
  key coverage;
- `RiskDecision` audit completeness (all fields non-empty, code = first failing rule, limits
  snapshot matches config);
- pipeline integration: risk evaluated **exactly once** per intent (trail counting), adapter
  never touched on rejection with the deciding code journaled, gate/connection checks still
  enforced around risk, reason-string contracts, and a provider-agnostic source scan of
  `backend/risk/*.py`.

Verification (Phase 8): `395 passed, 150 subtests`; `tsc --noEmit` clean; `npm run build`
succeeds; HTTP smoke passed (46 checks: accepted trade → position; reward:risk < 2, wrong-side
stop, zero/missing stop, duplicate symbol, stale timestamp, tightened exposure, and kill switch
each rejected with the right code while exactly 1 order row and 5 risk-decision rows reached the
journal; owner emergency-stop/reset round trip; no secret in any response).
