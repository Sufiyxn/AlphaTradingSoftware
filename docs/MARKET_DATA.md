# Market Data (Phase 5)

Read-only market data for paper trading. Two vendors are integrated — **Polygon** and
**Alpha Vantage** — both selected through the same `MarketDataProvider` interface, so the rest of
the system (scanner, bridge, valuation, backtest) never changes when the provider does.

Nothing here fabricates data: if a provider cannot answer, the platform reports an explicit error
state or an empty result. `UNPRICED` stays `UNPRICED`; no price is ever invented to fill a gap.

## Configuration

Everything is configured from environment variables (see `.env.example`), then restarted:

```bash
MARKET_DATA_PROVIDER=polygon          # polygon | alphavantage | none
POLYGON_API_KEY=...                   # required by MARKET_DATA_PROVIDER=polygon
# or
MARKET_DATA_PROVIDER=alphavantage
ALPHAVANTAGE_API_KEY=...              # required by MARKET_DATA_PROVIDER=alphavantage

MARKET_DATA_TIMEFRAME=1min            # polygon: 1min 5min 15min 1h 1day
                                      # alphavantage: 1min 5min 15min 30min 60min daily
MARKET_DATA_STALE_AFTER_SECONDS=300   # quote freshness window
MARKET_DATA_TIMEOUT_SECONDS=10        # per-request timeout
MARKET_DATA_BASE_URL=                 # optional proxy/override; defaults to the vendor API
```

Rules enforced by the config layer:

- Each vendor only reads **its own** key; a Polygon key can never mark Alpha Vantage configured.
- `MOCK`, `TEST`, `LOCAL`, and `FIXTURE` normalise to `none`. The provider registry never
  selects a test fixture, even if one is registered.
- An unknown timeframe or a non-positive timeout/staleness value fails configuration loudly
  instead of silently falling back.
- Secrets are never returned by any endpoint, never logged, and never stored in SQLite — status
  output shows presence booleans only.

## Capabilities

| Provider | Historical OHLCV | Latest quotes | Symbol lookup | Notes |
| --- | --- | --- | --- | --- |
| Polygon | `GET /v2/aggs/ticker/{symbol}/range/...` (paginated, max 10 pages) | `GET /v2/last/trade/{symbol}` | `GET /v3/reference/tickers/{symbol}` | Half-open range `[start, end)`, ascending, deduplicated |
| Alpha Vantage | `TIME_SERIES_INTRADAY` / `TIME_SERIES_DAILY` (`outputsize=full`) | `TIME_SERIES_INTRADAY` (`outputsize=compact`, latest bar close) | `SYNTAX_SEARCH` | Exchange-local timestamps converted from `America/New_York` to UTC |

Bid/ask are not published by these endpoints, so `Quote.bid`/`Quote.ask` stay `None` rather than
being estimated.

## Connection states

Credentials are never treated as connectivity. `GET /api/connections` (`market_data.connectivity`)
reports the state recorded from real evidence:

| State | Meaning |
| --- | --- |
| `NOT_CONFIGURED` | No provider/credentials; no request is ever made. |
| `UNAVAILABLE` | Configured, but no request has succeeded yet (or a timeout/reachability failure, or an expired rate-limit window). |
| `CONNECTING` | A request is currently in flight. |
| `CONNECTED` | A real request has succeeded. |
| `STALE` | The last success is older than `MARKET_DATA_STALE_AFTER_SECONDS`. |
| `RATE_LIMITED` | The provider throttled a request; `rate_limited_until` is set when the provider says when the window resets. |
| `ERROR` | Authentication was rejected, or the response body broke the documented schema. |

"Test connection" (`POST /api/connections/test`, owner token required) runs a real backend probe
(ticker lookup / symbol search). It is a **backend** request; the browser never calls a vendor.
When nothing is configured the test returns `NOT_CONFIGURED` with
`...; no network request was made`.

## Errors are explicit, never silent

Failures map to typed errors surfaced through `/api/status`, `/api/positions`, and the connection
section — always with a reason, never with substitute data:

| Condition | Error | State |
| --- | --- | --- |
| HTTP 401/403 or key-rejecting body | `ProviderAuthenticationError` | `ERROR` |
| HTTP 429 / throttle body (`Note`, `Information`) | `ProviderRateLimitError` | `RATE_LIMITED` |
| Timeout | `ProviderTimeoutError` | `UNAVAILABLE` |
| Unreachable host | `ProviderConnectionError` | `UNAVAILABLE` |
| HTTP 5xx | `ProviderUnavailableError` | `UNAVAILABLE` |
| Non-JSON body, `{}`, unexpected schema, broken fields | `ProviderResponseError` | `ERROR` |
| Unknown symbol / no rows | empty result (`()` or skipped quote) | unchanged |

Error text passes through a redactor that strips the API key before it reaches logs, payloads, or
the journal.

## Where the data flows

- **Position marking** — `MarketDataBridge` → `PositionValuationService`. Quotes older than the
  freshness window are `STALE` (real price, automatic exits paused); a missing quote is `UNPRICED`
  (`current_price: null`).
- **Scanner/orchestrator** — bars are required for a setup; zero bars means no candidate, not a
  guessed price.
- **Backtest** — `GET /api/backtest` runs only on bars actually retrieved from the connected
  provider; when none are retrieved it answers `{"status": "NO_DATA", "reason": ...}` instead of
  claiming a result.
- **Quote API** — `GET /api/quotes` returns the provider id, the honest state, and the quotes.

## Development and tests never hit the network

Adapters reach the network only through `backend/market_data/http.py`'s injectable transport.
Tests inject `backend/testing/http.py` (scripted responses; an unexpected request fails the test),
and the CI/dev smoke runs with no provider keys configured, so no vendor API is contacted.

## Troubleshooting

- **State stays `UNAVAILABLE` after saving the key** — expected until the first successful
  request or "Test connection".
- **`RATE_LIMITED`** — wait for `rate_limited_until` (or a minute on free tiers); the scanner
  keeps running and reports the reason instead of missing data silently.
- **`ERROR` with "authentication failed"** — the key is wrong for the selected provider; each
  provider only reads its own key variable.
- **Backtest answers `NO_DATA`** — no provider is configured, or the provider returned no rows
  for the window; no backtest runs without real bars.
