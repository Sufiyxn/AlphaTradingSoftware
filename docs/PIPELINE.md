# Canonical Trading Pipeline (Phase 7)

The one path from an execution request to an order. Every signal source — a TradingView alert,
the orchestrator's scanner cycle, an internal caller — hands an `ExecutionIntent` (or a
pre-approved `RiskDecision`) to `PaperTradingEngine.submit_decision`, which runs it through
`TradingPipeline`. Nothing else in the product calls `place_order`.

- Code: `backend/pipeline/` (`models.py`, `service.py`)
- Entry point: `backend/paper_trading/engine.py` (`submit` / `execute` / `submit_decision`)
- Signal paths: `backend/tradingview/service.py::_execute_entry`,
  `backend/orchestrator/service.py::_run_cycle`
- Wiring: `backend/core/runtime.py` — the paper engine shares the runtime's
  `ConnectionManager`, so the Connection Center reports the same connection the orders use
- Tests / proof: `backend/tests/test_pipeline.py`

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
       5. risk evaluation         - RiskEngine against the live portfolio snapshot, then the
                                    signal's quantity cap (reduce-only); journaled exactly once
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

Verification (Phase 7): `327 passed, 121 subtests`; `tsc --noEmit` clean; `npm run build`
succeeds; HTTP smoke passed (signed webhook → `202` → position; duplicate → `409`; journal holds
exactly one order and one risk decision; no secret in any response).
