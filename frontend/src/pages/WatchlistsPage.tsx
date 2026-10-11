/**
 * #/watchlists — the existing watchlist panel plus the monitored universe
 * and the market-data availability behind it.
 */
import { useState } from 'react'
import { EmptyState, ErrorState, LoadingState } from '../components'
import type { PageProps } from '../components'
import WatchlistPanel from '../WatchlistPanel'
import type { WatchlistPayload } from '../WatchlistPanel'
import { classifyApiPhase } from '../routes'
import type { PanelLoad } from '../types'

export default function WatchlistsPage({ status, statusPhase, statusError, onChartSymbolChange, refresh }: PageProps) {
  const [watchlist, setWatchlist] = useState<PanelLoad<WatchlistPayload>>({ data: null, error: '' })
  const phase = classifyApiPhase(watchlist.data !== null, watchlist.error !== '')

  const openInMarkets = (symbol: string) => {
    onChartSymbolChange(symbol)
    window.location.hash = '#/markets'
  }

  return <section className="page-two-col">
    <div>
      {phase === 'loading' && <LoadingState label="Loading the watchlist…" />}
      {phase === 'error' && <ErrorState what="Watchlist" message={watchlist.error} />}
      {/* The panel stays mounted (so it can fetch and report) but hidden until its first payload arrives. */}
      <div hidden={phase !== 'ready'}>
        <WatchlistPanel
          key={refresh}
          onSelect={openInMarkets}
          onLoad={(result) => setWatchlist((previous) => ({ data: result.data ?? previous.data, error: result.error }))}
        />
      </div>
    </div>
    <div className="panel health-panel">
      <div className="panel-head">
        <div>
          <p className="eyebrow">MONITORED UNIVERSE</p>
          <h2>Symbols on watch</h2>
        </div>
        <span className="count-badge">{status?.universe.length ?? 0}</span>
      </div>
      {statusPhase === 'loading' && <LoadingState label="Loading universe…" />}
      {statusPhase === 'error' && <ErrorState what="Universe" message={statusError} />}
      {status && (status.universe.length
        ? <div className="universe-chips">{status.universe.map((symbol) => <span className="universe-chip" key={symbol}>{symbol}</span>)}</div>
        : <EmptyState message="No universe symbols are configured. Set the scan universe in the backend configuration." />)}
      {status && <>
        <div className="health-row"><span>Market data</span><b>{status.market_data_provider} · {status.market_data_availability}</b></div>
        <div className="health-row"><span>Session</span><b>{status.market_session} · {status.market_session_orders_allowed ? 'orders allowed' : 'orders blocked'}</b></div>
        <p className="panel-foot">Click a row to chart the symbol on <a className="text-link" href="#/markets">Markets</a> · availability reason: {status.market_data_availability_reason ?? 'None recorded'}.</p>
      </>}
    </div>
  </section>
}
