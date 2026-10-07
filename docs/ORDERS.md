# Order Lifecycle & Reconciliation (Phase 9)

The platform's own model of an order — its identity, its status, its fills, and the evidence
behind every change — lives in `backend/orders/`. It is deliberately separate from any broker's
vocabulary: a provider reports what it believes, the lifecycle decides what that means, and
nothing is written down until the platform can prove it happened.

- Code: `backend/orders/` (`models.py`, `lifecycle.py`, `store.py`, `provider.py`,
  `reconcile.py`, `cancellation.py`, `service.py`)
- Written through by: `backend/pipeline/service.py` (the only path that submits)
- Read by: `ApplicationRuntime.list_orders` / `cancel_order` / `reconcile_orders` and the
  `GET`/`POST /api/orders*` endpoints
- Risk projection: `OrderStore.pending_reservations()` → `PortfolioSnapshot.pending_reservations`
- Tests / proof: `backend/tests/test_order_lifecycle.py`

## Three things that are never collapsed

| Concept | Type | Meaning |
|---|---|---|
| Request | `OrderIntent` | What the platform asks for. Exists before any provider sees it. |
| Order | `Order` | The request **plus** its lifecycle: an internal id, a provider id, a status, and the fills. Not a position. |
| Execution | `Fill` | One quantity at one price. The only thing that ever moves a position. |

Submitting an order never creates a position; only fills do.

## Identity

- `client_order_id` (aliased `order_id`) is minted by the platform — a `uuid4().hex` string.
  It is never a symbol, never a timestamp, and it survives restarts and provider
  re-identification.
- `provider_order_id` is whatever the provider answered with. It is `None` until submission
  succeeds, is stored verbatim, and a later answer that omits it never clears it. The provider's
  id never replaces the internal one.
- `Order.request` is the provider-facing `OrderRequest` rebuilt from the intent, kept so the
  journal's `record_order(order)` contract (`order_id`, `created_at`, `request.symbol`,
  `status.value`) is satisfied by one object.

## State machine

```
NEW ──▶ SUBMITTED ──▶ ACCEPTED ──▶ PARTIALLY_FILLED ──▶ FILLED
 │          │            │               │
 │          ├▶ PENDING ──┤               │
 │          │            └───────────────┤
 └▶ REJECTED / CANCELLED / FAILED        │
              (terminal, never moves) ◀──┘
```

- `NEW` exists **only** as a pre-submission state. `normalize_order_status("new")` still maps a
  provider's "new" word onto `ACCEPTED`, so a provider can never manufacture a `NEW` order.
- `TERMINAL_STATUSES = {FILLED, CANCELLED, REJECTED, EXPIRED, FAILED}` — `next_states()` for any
  of them is the status itself.
- `can_transition` / `with_status` refuse an illegal move by raising `InvalidOrderTransition`
  instead of jumping status: a filled order cannot be cancelled and a cancelled order cannot
  become filled.
- Re-applying the status a provider already reported is a no-op apart from `updated_at`, so
  polling stays idempotent.
- `UNKNOWN` is reachable from any non-terminal state and can be replaced by a known state; a
  terminal state can never become `UNKNOWN`.

Every transition goes through `backend/orders/lifecycle.py`, which is pure: immutable `Order`
in, immutable `Order` out, no store, no journal, no provider, no clock.

## Fills and quantity

`filled_quantity` and `average_fill_price` are always recomputed from the recorded fills:

```
filled_quantity    = Σ fill.quantity
average_fill_price = Σ (fill.quantity × fill.price) / filled_quantity   (None when there are no fills)
remaining_quantity = max(0, intent.quantity − filled_quantity)
```

- Fills are de-duplicated by `provider_fill_id` when the provider has one, else by `fill_id`.
  Replaying the same execution cannot double-count.
- Partial fill → `PARTIALLY_FILLED`; completing the quantity → `FILLED`.
- A provider that claims **more** executed quantity than the fills it described has that gap
  journaled as `RECONCILIATION_DISCREPANCY` (`note_fill_gap`). The extra quantity is **never**
  turned into a fill: an execution the provider will not describe cannot be priced, and
  inventing one would corrupt the average price and every position booked from it.
- A fill that arrives after a terminal state is recorded and journaled as a discrepancy; the
  terminal status itself is not rewritten.
- Submitting an order creates no fill at all.

## Open orders reserve risk capacity

An order that has been submitted but not (fully) filled commits capital the moment it fills, so
the risk layer reserves it. `OrderStore.pending_reservations()` returns one
`PendingOrderReservation` per open order for its **remaining** quantity at the price the order
itself carries (`limit` first, then `stop`, else no notional — a reservation never invents a
price but still holds the symbol and position slot).

`TradingPipeline._risk_snapshot()` attaches them to the `PortfolioSnapshot` before risk runs, so
a snapshot handed straight to `RiskEngine` in a test stays exactly what the portfolio produced
— only the canonical pipeline carries open orders into a risk evaluation.

Inside `RiskEngine`:

- sizing subtracts `reserved_notional` from the portfolio-exposure room, the cash term, and the
  symbol room (`backend/risk/sizing.py`);
- max positions counts `len(positions) + len(pending_symbols − held_symbols)`;
- a symbol with a pending order counts as open (`DUPLICATE_SYMBOL`);
- the final exposure invariant uses `projected = exposure + pending_notional + notional`;
- `RiskDecision.pending_exposure` records what was reserved so the decision can be replayed.

The whole path — risk sizing → adapter → order registration — runs under one re-entrant
`TradingPipeline.submission_lock`, and `PaperTradingEngine.execute`/`submit_decision` re-enter it
while booking the fill. Two intents can therefore never size themselves against the same
capacity.

## Journal events

`ORDER_EVENTS` is the vocabulary:

| Event | Meaning |
|---|---|
| `ORDER_CREATED` | the platform decided to execute; the provider has not been told yet |
| `ORDER_SUBMITTED` | the request reached the provider, which identified it |
| `ORDER_STATUS_CHANGED` | a lifecycle transition applied from a provider answer |
| `ORDER_FILL_RECEIVED` / `ORDER_PARTIAL_FILL` | one more execution was recorded |
| `ORDER_REJECTED` / `ORDER_CANCELLED` | terminal outcomes |
| `ORDER_SUBMISSION_FAILED` | the provider could not be reached; no order state was invented |
| `ORDER_CANCELLATION_FAILED` | a cancellation attempt did not result in a cancellation |
| `RECONCILIATION_DISCREPANCY` | the two sides disagree, or the provider could not be read |
| `RECONCILIATION_CLEAN` | a complete, error-free, difference-free comparison |

Orders are upserted into the `orders` table (`context_json` = the serialized order) and fills
into `order_fills` with `INSERT OR IGNORE` on the fill's own identity, so a replayed provider
answer is still exactly one row.

### A failed submission records no order

`TradingPipeline` registers the canonical order **after** the adapter has answered. If the
adapter raises, nothing is added to the store — the platform never records an order it cannot
prove exists — and only `ORDER_SUBMISSION_FAILED` is journaled with the intent context
(`note_submission_failure`). The observable tradeoffs are deliberate: a crash in the window
between a successful provider call and registration would lose that order, and a rejected
submission leaves capacity free again rather than reserving for something that never happened.

## Reconciliation

`OrderReconciler.reconcile(snapshot, *, positions=(), now=None)` is **read-only**: it compares
and reports, it never rewrites an order, never books a fill, never fixes a position.

A clean answer is only allowed when the snapshot said it was trustworthy:

| Situation | Status | `discrepancies` |
|---|---|---|
| provider raised / returned an error | `PROVIDER_ERROR` | empty |
| `complete=False` (partial page, timeout, filtered subset) | `INCOMPLETE` | empty |
| complete and error-free, no differences | `MATCH` | empty |
| complete and error-free, at least one difference | `DISCREPANCIES` | non-empty |

Absence of findings in the first two rows means **unknown**, never **fine** — `report.clean` is
`True` only for `MATCH`, and `report.unresolved` is `True` for `PROVIDER_ERROR`/`INCOMPLETE`.

`DiscrepancyKind` values: `LOCAL_ORDER_MISSING_REMOTELY`, `REMOTE_ORDER_UNKNOWN_LOCALLY`,
`STATUS_MISMATCH`, `QUANTITY_MISMATCH`, `PARTIAL_FILL_MISMATCH`, `FILL_MISSING_LOCALLY`,
`DUPLICATE_FILL`, `DUPLICATE_PROVIDER_ORDER`, `TERMINAL_LOCAL_OPEN_REMOTELY`,
`POSITION_QUANTITY_MISMATCH`, `POSITION_SIDE_MISMATCH`, `POSITION_UNKNOWN_LOCALLY`,
`POSITION_MISSING_REMOTELY`.

Notes on honesty:

- an open local order absent from the provider is a finding, but a `NEW` order (never handed
  over) and a terminal order are not expected remotely;
- a terminal local order that the provider still shows open is reported even though a status
  comparison would otherwise look like a plain mismatch;
- position drift is only compared when the provider actually returns a position book — paper
  adapters keep holdings in the portfolio subsystem, so an empty book tells us nothing and is
  never treated as "the provider has no positions".

`backend/orders/provider.py` defines the trust-carrying `ProviderStateSnapshot`
(`orders`/`fills`/`positions` + `complete` + `error`), the `RemoteOrder`/`RemoteFill`/
`RemotePosition` shapes, and `snapshot_from_adapter(...)`, which turns a failed read into a
snapshot that says so instead of an empty one.

## Cancellation

Cancelling is a request, not a command. `OrderCanceller.cancel(order_id)` returns one
`CancellationOutcome`:

| Outcome | Meaning | Success |
|---|---|---|
| `CANCELLED` | the order is now cancelled | yes |
| `ALREADY_CANCELLED` | idempotent second cancel | yes |
| `NOT_CANCELABLE` | terminal (filled/rejected/expired), or the provider filled it first | no |
| `REJECTED_BY_PROVIDER` | refused, unsupported, or answered with a status the order cannot move to | no |
| `PROVIDER_ERROR` | unreachable / not identified / no execution connection | no |
| `ORDER_NOT_FOUND` | no such local order | no |

Two invariants: a provider failure **never** changes local state (the order stays open, stays
reserved, stays visible), and a `NEW` order is closed locally without any provider call because
nothing was ever handed over. Each attempt journals `ORDER_CANCELLED` or
`ORDER_CANCELLATION_FAILED`.

## Restart recovery

`OrderLifecycleService.restore()` rebuilds the store from `journal.rows("orders")` — later rows
win because `record_order` upserts. A row that cannot be parsed is skipped rather than crashing
startup, but it is never dropped from the journal, so the order stays visible to
reconciliation. The service restores automatically when it is constructed with a journal, and
again the moment a host attaches one later (`attach_journal`), which is how the pipeline
recovers open orders after a restart.

## API surface

- `GET /api/orders` — `{"orders": [...describe()], "count", "open_count",
  "pending_reservation_count"}`; `?open=true` and `?symbol=MSFT` filter.
- `POST /api/orders/cancel` with `{"order_id": "<client_order_id>"}` (owner token) —
  `200` success (`CANCELLED`, `ALREADY_CANCELLED`), `400` missing id, `404`
  `ORDER_NOT_FOUND`, `409` every other non-success.
- `POST /api/orders/reconcile` (owner token) — `200` with the report; read-only.

`describe()` is the JSON-ready, secret-free view used by the API and the audit trail.

## Tests

`backend/tests/test_order_lifecycle.py` (66 tests):

- **state machine** — the documented chain, every terminal status final, illegal transitions
  raising, idempotent re-application, `UNKNOWN` recovery, and the `NEW` vs provider-"new"
  separation;
- **identity** — uuid-shaped client ids, `provider_order_id` only from the provider, the
  journal-compatible `request`/`order_id`, `reference_price`, and a `describe()` that is
  JSON-ready and carries no secret;
- **fill accounting** — sum, volume-weighted average, partial → filled, no fill at submit,
  replay de-duplication, the un-inventable quantity gap, and a late fill on a cancelled order;
- **pipeline integration** — `NEW → SUBMITTED → FILLED`, `SUBMITTED` for an accepted-but-unfilled
  order, a failed submission leaving no order while still being auditable, risk rejections never
  reaching the store, reservation of duplicate-symbol and max-position capacity, capacity
  released on fill, and two concurrent intents sizing against the same open orders;
- **pending reservations** — sizing terms shrinking with reserved notional, price-less
  reservations still holding their symbol, terminal orders reserving nothing;
- **cancellation** — all six outcomes, idempotency, and "a provider failure never changes local
  state";
- **reconciliation** — `MATCH`, status/quantity drift, `PROVIDER_ERROR`/`INCOMPLETE` never a
  match, missing/unknown/duplicate orders, missing provider fills, terminal-vs-open, position
  drift only with a book, no mutation, and journaling;
- **persistence** — order/fill/event rows, restart restore (status, provider id, fills,
  reservations), and concurrent store writes;
- **HTTP** — `GET /api/orders`, owner-token enforcement, each cancel status code, and a
  read-only reconcile;
- **architecture** — `backend/orders/**` never contains `place_order(`, and the package
  re-exports the canonical vocabulary.

Verification (Phase 9): `462 passed, 150 subtests`.
