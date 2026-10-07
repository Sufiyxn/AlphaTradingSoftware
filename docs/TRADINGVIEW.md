# TradingView Integration (Phase 4)

TradingView alerts are accepted at a dedicated local webhook, authenticated with their own
shared secret, validated and normalized, checked for duplicates, then handed to the canonical
trading pipeline (`docs/PIPELINE.md`). TradingView is a **signal source only**: it is not a
broker, not a market-data provider, and not a price source.

- Endpoint: `POST /api/webhooks/tradingview` (loopback only, no owner token required)
- Code: `backend/tradingview/` (auth, payload, service) and `backend/signals/` (normalized
  signal layer)
- Tests: `backend/tests/test_tradingview_webhook.py`

There is **no direct TradingView → broker execution**. Every accepted entry becomes an
`ExecutionIntent` (with the signal's timestamp, age limit, quantity cap, and `signal_id` as the
idempotency key) and travels `TradingPipeline`: connection verification, `RiskEngine.evaluate()`,
the execution safety gate, and the verified adapter. Nothing can bypass risk or execution
controls; the webhook's HTTP contract (below) is a mapping of pipeline outcomes.

## Enable it

```dotenv
TRADINGVIEW_ENABLED=true
TRADINGVIEW_WEBHOOK_SECRET=<long random value>
# Optional allowlist; blank accepts any supported strategy identifier.
TRADINGVIEW_ALLOWED_STRATEGIES=
# Rejects alerts older than this many seconds (default 300) or more than 60s in the future.
TRADINGVIEW_MAX_SIGNAL_AGE_SECONDS=300
# CONNECTED requires an accepted signal inside this window (default 86400 seconds).
TRADINGVIEW_STALE_AFTER_SECONDS=86400
# Largest request body in bytes, 1..65536 (default 8192); larger bodies get 413.
TRADINGVIEW_MAX_BODY_BYTES=8192
```

Restart the backend after changing `.env`. With the endpoint disabled or the secret unset, the
route answers `503 NOT_CONFIGURED` and `/api/connections` reports `NOT_CONFIGURED`.

## Authentication

The endpoint authenticates with `TRADINGVIEW_WEBHOOK_SECRET` alone and never requires
`OWNER_CONTROL_TOKEN`. Two equivalent mechanisms:

1. **HMAC header** (recommended): `X-Webhook-Signature: sha256=<hex>` where the hex digest is
   `HMAC-SHA256(secret, raw_request_body)`.
2. **Payload secret**: include `"secret": "<TRADINGVIEW_WEBHOOK_SECRET>"` in the JSON body.

If a signature header is present it is verified and the payload secret is ignored; if no
signature header is present the payload secret is required. Both comparisons are
constant-time. Wrong or missing credentials return `401 AUTH_FAILED` with no detail about
which check failed. The secret is never logged, never returned by any API, and never stored
in SQLite.

PowerShell example:

```powershell
$secret = 'your-webhook-secret'
$body = '{"symbol":"AAPL","action":"buy","entry_price":50.0,"stop_loss":48.0,"take_profit":54.0,"timestamp":"2024-01-02T15:29:00Z","quantity":5}'
$bytes = [Text.Encoding]::UTF8.GetBytes($body)
$hmac = [Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($secret))
$signature = ($hmac.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join ''
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/webhooks/tradingview' `
  -ContentType 'application/json' -Body $bytes `
  -Headers @{ 'X-Webhook-Signature' = "sha256=$signature" }
```

In a TradingView alert message, use the webhook URL and a JSON template containing the fields
below plus `"secret"` if you are not sending an HMAC header.

## Payload

A JSON object, UTF-8 encoded, at most `TRADINGVIEW_MAX_BODY_BYTES` bytes. Common alert field
names are accepted as aliases:

| Canonical field | Accepted JSON names | Required |
|---|---|---|
| `symbol` | `symbol`, `ticker`, `instrument`, `tradingsymbol` | yes |
| `action` | `action`, `side`, `signal_action`, `order_action`, `alert_action`, `signal` | yes |
| `timestamp` | `timestamp`, `time`, `datetime`, `alert_time`, `ts`, `created_at` | yes |
| `entry_price` | `entry_price`, `entry`, `open_price`, `price`, `alert_price` | entry signals |
| `stop_price` | `stop_price`, `stop`, `stop_loss`, `stoploss`, `sl` | entry signals |
| `target_price` | `target_price`, `target`, `take_profit`, `takeprofit`, `tp` | entry signals |
| `quantity` | `quantity`, `qty`, `size`, `shares`, `contracts` | no |
| `reported_price` | `price`, `exit_price`, `alert_price`, `close`, `last` | exit signals |
| `strategy` | `strategy`, `strategy_name`, `alert_name`, `alert`, `study` | only when an allowlist is set |
| `signal_id` | `signal_id`, `alert_id`, `event_id`, `id`, `alertid`, `client_id` | no |

Actions: `ENTER_LONG` (`buy`, `long`, `go_long`, `entry_long`, `buy_to_open`),
`ENTER_SHORT` (`short`, `sell_short`, `go_short`, `entry_short`, `short_sell`), and
`EXIT` (`sell`, `sell_to_close`, `close`, `close_position`, `exit`, `flat`, `cover`,
`close_all`).

Entry example:

```json
{"symbol":"AAPL","action":"buy","entry_price":50.0,"stop_loss":48.0,
 "take_profit":54.0,"quantity":5,"timestamp":"2024-01-02T15:29:00Z",
 "strategy":"tv_ema_cross","secret":"your-webhook-secret"}
```

Exit example (closes the full open position; `quantity` is optional and must match when
present):

```json
{"symbol":"AAPL","action":"close","price":52.5,"timestamp":"2024-01-02T16:10:00Z",
 "secret":"your-webhook-secret"}
```

## Validation rules

- `symbol` must match `^[A-Z][A-Z0-9.\-]{0,11}$` after uppercasing and must be in the
  configured universe (`UNIVERSE`, default `AAPL,BAC,F,PLTR,SOFI`).
- Entry prices must be positive and strictly below `MAX_CANDIDATE_PRICE` (100).
- `quantity`, when supplied, must be a whole number of shares from 1 to 1,000,000. It is an
  upper bound only: the risk decision's quantity is capped down to it, never widened by it.
  When omitted, normal risk sizing decides the quantity.
- `timestamp` must be ISO-8601 (a trailing `Z` is accepted) or a UNIX epoch value. A value
  without an offset is read as UTC. More than 60 seconds in the future, or older than
  `TRADINGVIEW_MAX_SIGNAL_AGE_SECONDS`, is rejected.
- A strategy allowlist (`TRADINGVIEW_ALLOWED_STRATEGIES`, comma-separated, compared
  case-insensitively) makes `strategy` mandatory and enforced.
- Exit signals require an explicit `reported_price` and an open position; the position is
  closed in full at that reported price.

## Responses

Every response is JSON: `{"status", "code", "message", "reason", ...}`.

| HTTP | `code` | Meaning |
|---|---|---|
| 202 | `ACCEPTED` | Signal verified and processed through risk and the paper workflow |
| 400 | `INVALID_PAYLOAD` | Malformed/empty body, missing or invalid field, unsupported symbol/action/strategy, invalid or stale timestamp, invalid quantity, entry at or above the price ceiling (`reason` names the specific cause) |
| 401 | `AUTH_FAILED` | Missing or wrong HMAC signature or payload secret |
| 409 | `DUPLICATE_SIGNAL` | The same event id or body was already processed; no second order |
| 413 | `PAYLOAD_TOO_LARGE` | Body exceeds `TRADINGVIEW_MAX_BODY_BYTES` |
| 422 | `RISK_REJECTED` / `NO_OPEN_POSITION` / `QUANTITY_MISMATCH` | Business rejection; nothing executed |
| 503 | `NOT_CONFIGURED` / `EXECUTION_UNAVAILABLE` | Endpoint not enabled/configured, or the paper engine could not fill |
| 500 | `INTERNAL_ERROR` | Unexpected failure; nothing further was attempted |

An `ACCEPTED` entry response includes `signal_id`, `symbol`, `action`, `order_id`,
`quantity`, `entry_price`, `stop_price`, `target_price`, `risk_approved_quantity`, and
`position_price_state` / `position_market_price`. An `ACCEPTED` exit response includes
`closed`, `exit_price`, `realized_pnl`, and `quantity`.

## Idempotency and replay protection

- Every delivery is keyed by `body:<sha256 of the exact bytes>` plus `id:<explicit id>` when
  the payload supplies one.
- Accepted signals and terminal business rejections (422) are remembered in the SQLite event
  journal as `SIGNAL_PROCESSED`; a replay, including after a restart, returns `409` and
  creates no duplicate order. Auth failures, validation failures, and 5xx outcomes are not
  remembered, so a corrected retry is processed normally.
- Events are journaled as `TV_SIGNAL_RECEIVED`, `TV_SIGNAL_REJECTED`, and
  `TV_SIGNAL_DUPLICATE`.

## Pricing limits (invariant)

The alert price is **signal/entry metadata only**. It is never written into a position's
ongoing market price. Opening a position does not make it priced: without a connected
market-data provider the position stays `UNPRICED` (`current_price: null`,
`unrealized_pnl: null`), automatic stop-loss/take-profit evaluation stays off, and the
alert's `price` is used only as the reported exit price when you explicitly send an exit
signal. See `backend/market_data/bridge.py` and the README "Data and trading limits"
section.

## Connection state

`GET /api/connections` → `tradingview` reports `state`, `reason`, `endpoint_status`,
`signals_accepted` / `signals_rejected` / `signals_duplicate`, `last_received_signal`, and
the limits above. `POST /api/connections/test` with `{"section": "tradingview"}` runs a local
authentication self-check and makes **no network request**.

| State | Meaning |
|---|---|
| `NOT_CONFIGURED` | Disabled or no secret set |
| `UNAVAILABLE` | Installed and listening, but no signal has been authenticated yet |
| `CONNECTED` | At least one accepted signal inside `TRADINGVIEW_STALE_AFTER_SECONDS` |
| `STALE` | The newest accepted signal is older than that window |
| `ERROR` | The most recent delivery failed with an internal error |

A configured secret alone never reports `CONNECTED`.

## Scope

Not in this phase: TradingView as a price source, direct broker execution from an alert,
TradingView charting, market-data providers (Phase 5), and the Connection & Execution Provider
Framework (Phase 6). TradingView remains required for the initial testing/demonstration workflow
as a signal source after execution providers land; see `docs/PLAN.md`.
