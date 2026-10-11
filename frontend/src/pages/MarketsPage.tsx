/**
 * #/markets — live charts plus the observed-quote tape, with the market
 * session and data freshness from /api/status shown honestly.
 */
import { useApi } from '../useApi'
import { ErrorState, Health, LoadingState, time, usd } from '../components'
import type { PageProps } from '../components'
import ChartPanel from '../ChartPanel'
import { freshnessTone } from '../routes'
import type { Quote, QuotesPayload } from '../types'

const staleAfterLabel = (seconds?: number | null) => {
  if (seconds == null) return '—'
  if (seconds < 90) return `${Math.round(seconds)}s`
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`
  return `${Math.round(seconds / 3600)}h`
}

export default function MarketsPage({ status, statusPhase, statusError, chartSymbol, onChartSymbolChange, refresh }: PageProps) {
  const quotesApi = useApi<QuotesPayload>('/api/quotes', { trigger: refresh, pollMs: 30000 })
  const payload = quotesApi.data
  const quotes = payload?.quotes ?? []
  // The live socket's own report; /api/status is authoritative, /api/quotes
  // repeats it so the tape can show the same truth.
  const stream = status?.market_data_stream ?? payload?.stream ?? null

  return <>
    <section className="panel extra-section">
      <div className="panel-head">
        <div>
          <p className="eyebrow">MARKET SESSION &amp; DATA FRESHNESS</p>
          <h2>Session state</h2>
        </div>
        <span className={`status-tag ${status?.market_session_orders_allowed ? 'good' : 'quiet'}`}>{status?.market_session ?? 'UNKNOWN'}</span>
      </div>
      {statusPhase === 'loading' && <LoadingState label="Loading market session…" />}
      {statusPhase === 'error' && <ErrorState what="Market session" message={statusError} />}
      {status && <div className="session-strip">
          <Health name="Session" value={status.market_session} />
          <Health name="Session policy" value={`${status.market_session_policy} policy`} />
          <Health name="Order window" value={status.market_session_orders_allowed ? 'Orders allowed' : 'Orders blocked'} />
          <Health name="Trading day" value={status.market_trading_day ? 'Yes' : 'No'} />
          <Health name="Data availability" value={status.market_data_availability} danger={status.market_data_availability === 'ERROR'} />
          <Health name="Data detail" value={status.market_data_availability_reason ?? 'None recorded'} />
          <Health name="Last quote" value={time(status.market_data_last_quote_at ?? null)} />
          <Health name="Stale after" value={staleAfterLabel(status.market_data_stale_after_seconds)} />
        </div>}
    </section>

    <section className="panel extra-section">
      <div className="panel-head">
        <div>
          <p className="eyebrow">LIVE DATA STREAM · PUSH, NOT POLLING</p>
          <h2>Streaming connection</h2>
        </div>
        <span className={`status-tag ${stream?.streaming ? 'good' : 'quiet'}`}>{stream?.state ?? 'NOT_REPORTED'}</span>
      </div>
      {statusPhase === 'loading' && <LoadingState label="Loading stream state…" />}
      {statusPhase === 'error' && <ErrorState what="Market-data stream" message={statusError} />}
      {status && <div className="session-strip">
          <Health name="Stream state" value={stream?.state ?? 'NOT_REPORTED'} danger={stream?.state === 'ERROR' || stream?.state === 'AUTH_FAILED'} />
          <Health name="Feed entitlement" value={stream?.entitlement ?? '—'} />
          <Health name="Authenticated" value={stream?.authenticated ? 'Yes' : 'No'} />
          <Health name="Subscribed symbols" value={(stream?.symbols ?? []).join(', ') || '—'} />
          <Health name="Last live event" value={time(stream?.last_event_at ?? null)} />
          <Health name="Events received" value={String(stream?.events_received ?? 0)} />
          <Health name="Quotes accepted" value={String(stream?.quotes_received ?? 0)} />
          <Health name="Messages ignored" value={String(stream?.ignored_messages ?? 0)} />
          <Health name="Reconnects" value={String(stream?.reconnects ?? 0)} />
        </div>}
      {stream?.reason && <div className="empty-state">{stream.reason}</div>}
      <p className="panel-foot">
        {stream?.subscription ?? 'Not subscribed — a stream only appears when the configured provider ships a streaming adapter and its credentials are present.'}
      </p>
    </section>

    <section className="layout-grid">
      <ChartPanel symbol={chartSymbol} onSymbolChange={onChartSymbolChange} />
      <div className="panel watchlist-panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">QUOTE TAPE · OBSERVED TRADES ONLY</p>
            <h2>Latest quotes</h2>
          </div>
          <span className={`status-tag ${payload?.state === 'OK' ? 'good' : 'quiet'}`}>{payload?.state ?? '—'}</span>
        </div>
        {quotesApi.phase === 'loading' && <LoadingState label="Loading quotes…" />}
        {quotesApi.phase === 'error' && <ErrorState what="Quote tape" message={quotesApi.error} />}
        {quotesApi.phase === 'ready' && (quotes.length
          ? <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Symbol</th><th>Price</th><th>Bid</th><th>Ask</th><th>Volume</th><th>As of</th><th>State</th></tr>
                </thead>
                <tbody>
                  {quotes.map((quote: Quote) => {
                    const tone = freshnessTone(payload?.state)
                    return <tr key={quote.symbol}>
                      <td className="symbol">{quote.symbol}</td>
                      <td>{usd(quote.price)}</td>
                      <td>{quote.bid == null ? '—' : usd(quote.bid)}</td>
                      <td>{quote.ask == null ? '—' : usd(quote.ask)}</td>
                      <td>{quote.volume ?? '—'}</td>
                      <td>{time(quote.timestamp)}</td>
                      <td><span className={`state-tag ${tone}`}>{payload?.state ?? 'UNKNOWN'}</span></td>
                    </tr>
                  })}
                </tbody>
              </table>
            </div>
          : <div className="empty-state">No live quotes are available{payload?.reason ? ` — ${payload.reason}` : ''}. Quotes appear once a continuous market-data provider is connected in <a className="text-link" href="#/connections">Connections &amp; settings</a>.</div>)}
        <p className="panel-foot">{payload?.provider ?? '—'} · a quote row shows one observed trade price; nothing here is estimated or synthesized.</p>
      </div>
    </section>
  </>
}
