# Connections & Execution Providers (Phases 6–7)

The provider-agnostic framework that sits between the platform and any broker/trading platform a
client may connect. It answers three questions honestly and only from evidence:

1. **Is a connection configured?** (credentials present)
2. **Is it verified?** (authentication + account/permission actually succeeded)
3. **Can this build execute through it?** (an adapter exists and the safety gate allows the mode)

This phase is **not** an Alpaca or Robinhood integration. Alpaca and Robinhood appear only as
*advertised, not installed* providers, which is exactly what the dashboard shows.

- Code: `backend/connections/` (manager, registry, models, credentials, errors),
  `backend/execution/` (interface, capabilities, gate, models, local paper adapter)
- Order path (Phase 7): `backend/pipeline/` — orders travel this manager; see `docs/PIPELINE.md`
- Connection Center integration: `backend/core/connections.py` (`execution` section)
- Runtime wiring: `backend/core/runtime.py`
- Test adapters: `backend/testing/execution.py` (never imported by the runtime)
- Tests: `backend/tests/test_execution_connections.py`,
  `backend/tests/test_pipeline.py` (pipeline/manager proof)
- Frontend card: `frontend/src/ConnectionsPanel.tsx` ("Execution provider")

## Flow

What the framework establishes (connection state, verified by evidence):

```
Client connection credentials/details
  -> ConnectionManager (backend/connections/manager.py)
  -> provider identification (ExecutionProviderRegistry)
  -> authentication
  -> capability discovery
  -> account / permission verification
  -> readiness checks
  -> connection state + capabilities, reported by the Connection Center
```

What actually executes orders in this build (Phase 7):

```
signal / strategy / scan
  -> ExecutionIntent (backend/pipeline/models.py)
  -> PaperTradingEngine.submit_decision
  -> TradingPipeline (backend/pipeline/service.py)
       -> ConnectionManager resolve + verify   <- readiness evidence, fail closed
       -> RiskEngine
       -> ExecutionSafetyGate
       -> execution mode + capability checks
       -> connections.adapter_for(id).place_order(...)   <- the only order call
  -> normalized PipelineResult -> booked paper trade
```

The framework *is* the order path now: `ConnectionManager.adapter_for()` returns the verified
adapter the pipeline submits through, and the runtime shares one manager between the Connection
Center and the paper engine — the state the dashboard reports is the state orders travelled.
`ExecutionRouter` remains available as a composable utility but is no longer on the production
order path. See `docs/PIPELINE.md` for the step order and the deterministic rejection codes.

No strategy, signal source, AI advisory, or market-data provider ever learns a
broker's name; everything downstream of risk consumes `TradingPlatformAdapter` only.

## Connection states

`ConnectionState` (all nine are reachable and reported verbatim):

| State | Meaning |
|---|---|
| `NOT_CONFIGURED` | No credentials / nothing to connect with. |
| `CONFIGURED` | Credentials present; **not** verified. Credentials alone never reach `CONNECTED`. |
| `CONNECTING` | A verification is in progress. |
| `AUTHENTICATED` | Provider authentication succeeded; account not yet confirmed. |
| `CONNECTED` | Verification fully succeeded inside the freshness window. |
| `DEGRADED` | Connected but impaired (rate limited, elevated provider errors). |
| `UNAVAILABLE` | The provider host/adapter cannot be reached or is not installed. |
| `ERROR` | Authentication or account verification was rejected. |
| `STALE` | Last successful verification is older than `stale_after_seconds` (default 300). |

Supporting state vocabularies: `AuthenticationState` (`NOT_CONFIGURED`, `UNVERIFIED`,
`AUTHENTICATING`, `AUTHENTICATED`, `FAILED`), `AccountVerificationState` (`UNKNOWN`, `UNVERIFIED`,
`VERIFIED`, `FAILED`, `UNSUPPORTED` — `UNSUPPORTED` means the adapter genuinely cannot expose
account verification, and it never counts as verified), `PermissionState` (`UNKNOWN`, `GRANTED`,
`DENIED`).

### Readiness

`ReadinessResult.ready` requires **all** of: configured, authenticated, connected, account
verified (or honestly `UNSUPPORTED` + acknowledged in the failure reason), trading permission for
the mode, and an authorized execution mode. The failure reason is always a concrete sentence
("the Alpaca execution adapter is not installed; no network request was made", "authentication
has not been confirmed", "the last successful verification is older than 300 seconds").

## Providers shipped in this build

| Provider | Implemented | Modes | Credentials | Notes |
|---|---|---|---|---|
| `local_paper` | **yes** | `PAPER` | none | In-process paper account (cash, fills, positions). Performs **no network request**. |
| `alpaca` | no | `PAPER`, `LIVE` | `ALPACA_API_KEY`, `ALPACA_API_SECRET` | Advertised from `BROKER_CATALOG`; `build()` refuses it honestly. |
| `robinhood` | no | — | — | Example future provider; no adapter and no documented integration exists. |

Configuring an unimplemented provider (`.env`):

```dotenv
TRADING_PLATFORM_PROVIDER=alpaca        # also: TRADING_PLATFORM_ADAPTER
TRADING_PLATFORM_ENVIRONMENT=SANDBOX    # SANDBOX | LIVE
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
```

That connection reports `CONFIGURED` (or `UNAVAILABLE` after a test, with
"not installed; no network request was made") — never `CONNECTED`, never `READY`. Adding a real
provider means registering one `ExecutionProviderDescriptor` with an `adapter_factory` in
`backend/connections/registry.py`; strategies, risk, and the decision engine are untouched. If
automatic identification is impossible, the descriptor declares `credential_fields` and the client
identifies the provider explicitly.

## Capability discovery

`PlatformCapabilities` is read from the adapter at verification time and reduced to the platform's
capability vocabulary (`KNOWN_CAPABILITIES`: `equities`, `market_orders`, `limit_orders`,
`stop_orders`, `short_selling`, `fractional_shares`, `extended_hours`, `order_cancellation`,
`order_modification`, `positions`, `order_status`, `paper_trading`, `live_trading`, `market_data`,
`real_time_account_info`). Anything the adapter does **not** support is reported under
`capability_limitations` — never silently claimed. Declared catalog capabilities are labelled as
declared, not verified.

## Paper / live separation and the safety gate

`ExecutionSafetyGate` (`backend/execution/gate.py`) participates in readiness and safety
evaluation:

- `authorize(ExecutionMode.PAPER)` → allowed (unless the risk kill switch or the manual stop is
  engaged).
- `authorize(ExecutionMode.LIVE)` → **always denied in this build**: "no live execution adapter
  and no live activation control is installed; safety gates must open before live trading".

The gate also reads the `RiskEngine` kill switch (emergency stop latches it; reset is an explicit
owner action) and has its own manual latch. A connection can be `CONNECTED` with `PERMISSION
GRANTED` and still be `ready: false` because live mode is locked — configuration can never widen a
safety gate. `GET /api/readiness` reports `live_mode: LOCKED`.

The gate is consulted when a connection's readiness is computed, when the gate state is
reported, and on **every order submission**: `TradingPipeline` authorizes the execution mode
before it evaluates risk and again before it hands the order to the adapter, so an engaged kill
switch or a locked LIVE mode rejects with `KILL_SWITCH` / `SAFETY_REJECTED` before any provider
is touched (`docs/PIPELINE.md`). It cannot be bypassed because the order path reaches only
through that pipeline, no live adapter exists, and `RiskEngine` (including its kill switch) sits
inside every execution call. Since Phase 8 the same pipeline also enforces the full risk
hardening set — sizing, per-trade risk, 2:1 reward:risk, exposure/symbol/daily-loss limits,
duplicate-symbol and price sanity — as machine-readable `RiskDecision` codes journaled per
intent, so a connection can be fully verified and still never receive an order the risk layer
did not approve (`docs/PIPELINE.md`, "Risk controls").

## Orders

`OrderRequest` carries `time_in_force` (default `DAY`, blank rejected) and `execution_mode`
(default `PAPER`; `LIVE` only meaningful behind the gate). `Order` carries `provider_reference`
(the provider's own order id, `null` when local) and `updated_at`.

`OrderStatus`: `NEW`, `SUBMITTED`, `ACCEPTED`, `REJECTED`, `PARTIALLY_FILLED`, `FILLED`,
`CANCELLED`, `EXPIRED`, `FAILED`, `PENDING`, `UNKNOWN`. `normalize_order_status()` maps provider
words ("new", "partially-filled", "canceled", "failure", unknown junk) onto that vocabulary;
anything unrecognized becomes `UNKNOWN` — never `FILLED`, and a provider's "new" becomes
`ACCEPTED`, never the platform-only `NEW`.

`TradingPlatformAdapter` also defines `health_check()` (credentials alone are not connectivity)
and `reconcile_orders(since=...)` for provider-side order/fill reconciliation.

The order the platform owns — its internal `client_order_id`, its lifecycle status, and the
fills behind the quantity — is the canonical `backend.orders.Order` written through the
pipeline. The provider's `Order`/`OrderStatus` above is what an adapter returns; adoption,
fill de-duplication, cancellation outcomes, and read-only reconciliation against
`snapshot_from_adapter(...)` live in `backend/orders/` (`docs/ORDERS.md`).

## Multiple connections

Each client connection gets its own `connection_id`, provider, mode, environment, and credentials:

```python
manager.add(connection_id="paper", provider_id="local_paper", adapter=LocalPaperAdapter(...))
manager.add(connection_id="a", provider_id="alpaca", credentials=handle_a, verify=True)
manager.add(connection_id="b", provider_id="scripted", credentials=handle_b, verify=True)
```

States are tracked independently; `manager.aggregate_state()` returns one honest state for the set
(worst evidence wins: `CONNECTED` → `CONFIGURED` when an unverified connection exists →
`UNAVAILABLE` when one fails).

## API surface

- `GET /api/connections` → `execution_connections` section: `count`, `configured`, `ready`,
  `ready_count`, `state`, `reason`, `connections[]` (per-connection state, reasons,
  authentication/account/permission states, capabilities, limitations, readiness, **credential
  names + `configured` booleans only**), `providers[]` (registry descriptors; never a callable),
  and `gate` (`role: EXECUTION_SAFETY_GATE`, `paper_allowed`, `live_allowed`,
  `kill_switch_engaged`, `manual_stop_engaged`).
- `POST /api/connections/test` with `{"section": "execution"}` (owner token) → runs
  `verify_all()`; local-only connections append "no network request was made".
- `GET /api/readiness` → check `execution_connection_configured`, check
  `execution_connection_verified`, plus `execution_state`, `execution_reason`,
  `execution_ready_count`, `execution_gate`, and `live_mode`.
- `GET /api/status` → `execution_adapter` (the adapter the execution router is wired to, e.g.
  `local_paper`) and `readiness` (the overall readiness status, e.g. `LOCKED`). It does **not**
  repeat the execution connection fields; those are on `GET /api/readiness` and
  `GET /api/connections`.
- `GET /api/orders` → the canonical order lifecycle: `orders[]` (`describe()`, secret-free),
  `count`, `open_count`, `pending_reservation_count`; `?open=true` and `?symbol=` filter.
- `POST /api/orders/cancel` with `{"order_id": "<client_order_id>"}` (owner token) → `200` for
  `CANCELLED`/`ALREADY_CANCELLED`, `400` without an id, `404` `ORDER_NOT_FOUND`, `409` for every
  other non-success (`PROVIDER_ERROR`, `REJECTED_BY_PROVIDER`, `NOT_CANCELABLE`).
- `POST /api/orders/reconcile` (owner token) → read-only comparison of the journal with the
  connection's adapter; `200` with `status` `MATCH`/`DISCREPANCIES`/`PROVIDER_ERROR`/
  `INCOMPLETE`, `clean`, and the `discrepancies[]` findings (`docs/ORDERS.md`).
- The dashboard renders the same payload as the "Execution provider" card; the browser never
  contacts a provider.

## Secrets

Credentials are held in `CredentialHandle`, which exposes presence (`present()`, `missing()`,
`describe()`) and redacts values from any error text (`redact()`). No API payload, log line, test
report, or `repr()` ever contains a credential value — enforced by tests.

## Tests

`backend/tests/test_execution_connections.py` covers the registry (validation, unimplemented
providers, catalog consistency, factory never serialized), the lifecycle (configured ≠ connected,
missing credentials, unknown provider, auth/connectivity/timeout/rate-limit/account failures,
staleness, `CONNECTING`), capability discovery and limitations, the safety gate (paper allowed,
live locked, kill switch), credentials redaction, three independent connections, order/status
normalization, the Connection Center + HTTP endpoints, frontend/vendor separation (no vendor host
and no absolute `fetch` anywhere in `frontend/src`), and Phase 1–5 regression guards (frontend
vendor-host bans, market-data domain untouched, risk between decision and execution).

Verification (Phase 6 checkpoint): `314 passed, 102 subtests`; `tsc --noEmit` clean;
`npm run build` clean; no vendor calls; `git diff` reviewed. Committed at the Phase 6 checkpoint
as `Complete phase 6 execution provider framework`.
