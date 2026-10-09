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

### Durable fill identity (Phase 10 checkpoint 2)

Every fill is given exactly one durable identity before it is merged, journaled, or compared
(`backend/execution/fill_identity.py`):

- The provider's own number (`provider_fill_id`) is adopted verbatim when the provider supplied
  one — the paper venue stamps `<provider_order_id>-F<n>` itself — and otherwise the identity is
  the deterministic `FILL-<sha256>` of `client_order_id + provider_order_id + quantity + price +
  timestamp` over a documented canonical serialization, so the same execution derives the same
  identity after a JSON round trip and after a restart. A random identifier is never used.
- The identity is scoped `<connection_id>:<provider_instance_id>:<key>` — the same scope
  checkpoint 1 gave provider order identities — because a provider number is only unique for the
  connection that saw it: one number on two scopes is two fills, and the same fill twice in one
  scope is one fill.
- Identity is assigned once, in `with_fills`, the single door fills enter through, and is never
  re-derived or rewritten afterwards.
- Persistence is fail-closed. `record_provider_fill` returns whether it actually inserted the
  row, and a fill the journal refuses raises `FillPersistenceError` **before** the fill is
  merged — in `record_fill`, in `apply_provider_order`, and on the cancellation path — so a fill
  that is not durable is never visible in the order, in the API, or in the `orders` row a
  restart rebuilds from.

### Positions are booked from durable fills (Phase 10 checkpoint 3)

A position exists because executions happened, not because the pipeline said a word. Checkpoint 2
made fills durable; this checkpoint makes them the only thing that moves a position.

- The order lifecycle **announces** fills through `OrderLifecycleService.fill_observer`
  (`backend/orders/service.py`), called exactly once per fill with only the fills that became
  durable *for the first time* in that call — new to the order and new to the journal. A replay
  never reaches the observer, and a journal that cannot store fills stores nothing to replay
  against. Without a journal nothing is announced, and the paper engine still books from the
  order's own fills, because a missing journal is "no durable record", not "no execution".
- `PaperTradingEngine._on_durable_fills` is the only booking path. The first announcement for an
  order opens the trade on the setup the submission was risk-approved with; later announcements
  add to the open position. An order with no setup, a rejected order, and an order that never
  executed still open nothing: a position is never invented.
- Booking is keyed by `fill_id` (`_booked_fills`) and gated on the journal's insert-versus-replay
  answer together, so a late or partial execution books once — before a restart, and after one
  (`restore_trade` seeds the set from the fills the restored order already carries).
- The trade row follows the position: its `quantity` and `entry_price` are rewritten to what the
  position now holds, because `reconcile_journal` rebuilds cash as
  `starting_cash − Σ(open quantity × entry price) + Σ(closed pnl)`. Leaving the original entry
  price behind would drift the ledger on restart by exactly the later fills' cost.
- Fills the journal holds that no trade accounts for are reported at restart as
  `RECONCILIATION_DISCREPANCY` (`DURABLE_FILL_WITHOUT_TRADE` for an order with fills but no trade
  row, `DURABLE_FILL_WITHOUT_ORDER` for a fill whose order the journal cannot reconstruct) and
  are never turned into a position — divergence is surfaced, not papered over. The same
  disagreement seen live — a fill already durable that the order had never seen — is reported as
  `DURABLE_FILL_UNKNOWN_TO_ORDER`, and the order adopts the execution without re-announcing it.
- A durable fill row carries where it came from: `context_json` holds `order_id`, `side`, the
  order's `source` (which path created it) and its `signal_id` when there was one, so executions
  can be read from `/api/logs?kind=fills` alone.

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

Orders are upserted into the `orders` table (`context_json` = the serialized order) and provider
fills into `order_fill_journal`, whose primary key is `(connection_id, provider_instance_id,
fill_id)` with a partial unique index on `(connection_id, provider_instance_id,
provider_fill_id)`, written with `INSERT OR IGNORE` — so a replayed provider answer is still
exactly one row, and the insert's rowcount tells the caller whether the fill was new. The fills
are written *before* the order row that `restore` rebuilds from, so a refused fill cannot be
resurrected by a restart. The phase 9 `order_fills` table is left exactly as it was: checkpoint 2
adds a durable home for fills instead of migrating an existing one.

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

Who calls the provider: the pipeline supplies the executor. `_ConnectionExecutor`
(`backend/pipeline/service.py`) resolves the order's own connection in the shared store, asks the
`ConnectionManager` for that connection's verified adapter, and calls `cancel_order` on it — so a
submitted order is cancelled at the provider first and marked `CANCELLED` locally second. A
connection with no verified adapter raises into a `PROVIDER_ERROR`; it never closes the order
locally as if it had succeeded.

Every state change the canceller applies is written back to `journal.rows("orders")` in the same
call that changes it, so a restart cannot resurrect a cancelled order or leave it holding a
reservation. A cancellation that failed to reach the provider writes no row at all.

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
- `POST /api/positions/close` with `{"symbol": "AAPL", "price": 50.5}` (owner token) —
  `200` `{closed, symbol, quantity, remaining, partial, direction, requested_price,
  realized_pnl, reason: "MANUAL", execution: "PAPER"}`, `400` `INVALID_SYMBOL`/`INVALID_PRICE`
  for a missing symbol or a price that is absent, non-numeric, zero, negative or non-finite,
  `400` `INVALID_QUANTITY` for a quantity that is not a positive whole number **or** that
  exceeds what the position holds, `404` `POSITION_NOT_FOUND` when nothing is open for that
  symbol **or** it was already closed, `503` when owner controls are disabled. The price is
  always supplied by the caller — never defaulted, marked or derived from the entry — and the
  close runs under the submission lock with the same slippage, fee and journal accounting as
  an automatic or signal exit. An optional `quantity` scales the exit down to a **partial
  close**: the remainder keeps its entry price, `partial: true` with `remaining` reports how
  much is left, and omitting `quantity` closes everything exactly as before.
- `POST /api/positions/risk` with `{"symbol": "AAPL", "stop_price": 49.5, "target_price": 55}`
  (owner token) — `200` `{changed: true, symbol, direction, entry_price, quantity,
  stop_price: {old, new}, target_price: {old, new}, reward_risk_ratio, execution: "PAPER",
  reason: "OWNER_ADJUSTMENT"}`; `400` `INVALID_PAYLOAD` when neither level is sent, `400`
  `INVALID_PRICE` for a non-positive or non-finite value, `400` `RISK_NOT_REDUCING` unless a
  stop move tightens it (long: strictly higher; short: strictly lower), `400`
  `STOP_THROUGH_ENTRY` / `TARGET_NOT_BEYOND_ENTRY` when a level crosses entry, `400`
  `REWARD_RISK_TOO_LOW` below the configured 2:1 floor, `404` `POSITION_NOT_FOUND` when
  nothing is open for that symbol, `503` when owner controls are disabled. Only the two
  levels move — entry price, quantity and fills are untouched — exactly one `RISK_CHANGED`
  position event carries old and new values, and the adjusted levels are what the automatic
  exit uses immediately and after a restart.

`describe()` is the JSON-ready, secret-free view used by the API and the audit trail.

## Tests

`backend/tests/test_order_lifecycle.py` (71 tests):

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
- **cancellation** — all six outcomes, idempotency, "a provider failure never changes local
  state", the pipeline's own service reaching the connection adapter, and a cancelled order
  staying cancelled (with no reservation) across a restart;
- **reconciliation** — `MATCH`, status/quantity drift, `PROVIDER_ERROR`/`INCOMPLETE` never a
  match, missing/unknown/duplicate orders, missing provider fills, terminal-vs-open, position
  drift only with a book, no mutation, and journaling;
- **persistence** — order/fill/event rows, restart restore (status, provider id, fills,
  reservations), and concurrent store writes;
- **HTTP** — `GET /api/orders`, owner-token enforcement, each cancel status code, and a
  read-only reconcile;
- **architecture** — `backend/orders/**` never contains `place_order(`, and the package
  re-exports the canonical vocabulary.

Verification (Phase 9): `467 passed, 150 subtests`.

`backend/tests/test_provider_fills.py` (23 tests) holds Phase 10 checkpoint 2: the provider's own
fill number adopted verbatim, the deterministic `FILL-` fallback, scope qualification and its
stability across serialization and a restart, one durable row per fill per provider scope, the
insert's new-vs-replay answer, and fail-closed persistence — including a fill refused during a
cancellation never becoming visible in the order, in the API, or in the row a restart rebuilds
from.

Verification (Phase 10 checkpoint 2): `509 passed, 150 subtests`.

`backend/tests/test_position_fills.py` (10 tests) holds Phase 10 checkpoint 3: a fill reported
after acceptance reaching the position and the trade record, replays booking nothing, an
order accepted without a fill opening its position when it fills, the initial submission
booking exactly once, the observer announcing only new-and-durable fills (with a
`DURABLE_FILL_UNKNOWN_TO_ORDER` report when the journal already held one), restart rebuilding
cash and quantity the fills describe with a post-restart replay booking nothing, a durable fill
no trade accounts for being reported instead of invented, the no-journal path still booking, and
an order that can no longer fill keeping nothing.
`test_tradingview_webhook.py` adds the demo evidence: the fill row's `source`/`signal_id`,
`GET /api/activity` carrying executions and risk decisions, and the webhook order, its
executions, and its position served over HTTP after a restart; `test_journal_reporting.py`
holds the fill export.

Verification (Phase 10 checkpoint 3): `523 passed, 150 subtests`.

`backend/tests/test_position_exits.py` (11 tests, 12 subtests) holds Phase 10 checkpoint 4: an
owner-authorized close of one open paper position through `POST /api/positions/close`, proved by
running the real accounting — the expected P&L recomputed from the position's weighted entry, the
configured slippage and commission rather than asserted as a literal — plus exactly one `CLOSED`
position event and one trade close per success, the entry order still `FILLED`, every impossible
price (absent, zero, negative, non-numeric, boolean, `NaN`, `inf`) refused with the position
untouched, unknown and already-closed symbols reported as `POSITION_NOT_FOUND`, the owner-token
guard, the `503` when owner controls are disabled, and no credential in any response.

Verification (Phase 10 checkpoint 4): `536 passed, 164 subtests`; HTTP smoke `50/50 checks`.

`backend/tests/test_position_exits.py` also holds Phase 10 checkpoint 5 (20 tests, 25 subtests
in the file): a partial close through the same owner endpoint leaving the remainder at its
weighted entry price with `unrealized_pnl: null` while it stays `UNPRICED`, stepwise closes
summing to exactly what one full close books (checked against the formula and against a
witness runtime), cash moving only by the proceeds it realized and the whole trade's cash
netting to its realized P&L, every over-close and non-whole quantity refused with state
untouched, the trade row staying open with `quantity` shrunk to what is held (and reporting
the whole trade once the last unit leaves, with P&L covering every step), one
`PARTIALLY_CLOSED` event per partial close, and a restart restoring the remainder, the cash
the partials booked, the daily P&L and the close-operation count.

Verification (Phase 10 checkpoint 5): `545 passed, 177 subtests`; HTTP smoke `53/53 checks`.

`backend/tests/test_position_risk.py` (13 tests, 14 subtests) holds Phase 10 checkpoint 6: a
tightened stop and moved target journaled with exactly one `RISK_CHANGED` event carrying old
and new values, entry/quantity/fills/setup proven untouched, every refusal refused with state
and journal unchanged (loosened or repeated stop, stop through entry, target that breaks the
2:1 floor, target on the entry side, impossible prices, empty payload, unknown symbol), the
new stop firing the automatic exit at a price the old stop would have ignored (with the
control proving the old stop would not), the same levels restoring and firing after a
restart, and the HTTP status mapping with no credential in the response.

Verification (Phase 10 checkpoint 6): `558 passed, 191 subtests`; HTTP smoke `58/58 checks`.
