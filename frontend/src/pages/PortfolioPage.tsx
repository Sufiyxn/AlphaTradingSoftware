/**
 * #/portfolio — open paper positions with honest valuation status: unpriced
 * positions stay unpriced, stale marks carry their age, and the portfolio
 * snapshot comes straight from /api/status.
 */
import { useApi } from '../useApi'
import { Empty, ErrorState, Health, LoadingState, Metric, StateBadge, age, usd } from '../components'
import type { PageProps } from '../components'
import type { PositionsPayload } from '../types'

export default function PortfolioPage({ status, refresh }: PageProps) {
  const positionsApi = useApi<PositionsPayload>('/api/positions', { trigger: refresh, pollMs: 10000 })
  const payload = positionsApi.data
  const positions = payload?.positions ?? []
  const account = status?.portfolio

  return <>
    <section className="metric-grid">
      <Metric label="Cash" value={usd(account?.cash)} note="Paper account" />
      <Metric label="Portfolio equity" value={usd(account?.equity)} note={`${payload?.valuation_status ?? status?.valuation_status ?? '—'} valuation`} />
      <Metric label="Open exposure" value={usd(account?.exposure)} note={`${positions.length} active positions`} />
      <Metric
        label="Unrealized P&L"
        value={usd(account?.unrealized_pnl)}
        note={payload?.valuation_status === 'PARTIAL' ? 'Valued positions only' : 'All open positions valued'}
        tone={(account?.unrealized_pnl ?? 0) >= 0 ? 'positive' : 'negative'}
      />
    </section>

    {positionsApi.phase === 'error' && <ErrorState what="Positions" message={positionsApi.error} />}
    {positionsApi.phase === 'loading' && <LoadingState label="Loading open positions…" />}

    {positionsApi.phase === 'ready' && <section className="layout-grid lower-grid">
      <div className="panel wide-panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">PAPER PORTFOLIO · {payload?.valuation_status ?? 'COMPLETE'} VALUATION</p>
            <h2>Open positions</h2>
          </div>
          <span className="count-badge">{positions.length}</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Symbol</th><th>Direction</th><th>Quantity</th><th>Entry</th><th>Mark</th><th>Stop</th><th>Target</th><th>P&amp;L</th><th>R</th><th>Status</th></tr>
            </thead>
            <tbody>
              {positions.map((p) => <tr key={p.symbol}>
                <td className="symbol">{p.symbol}</td>
                <td>{p.direction}</td>
                <td>{p.quantity}</td>
                <td>{usd(p.entry_price)}</td>
                <td>{p.current_price == null ? <StateBadge state={p.price_state} reason={p.price_reason} /> : <>{usd(p.current_price)}{p.price_state === 'STALE' && <> <StateBadge state={p.price_state} reason={p.price_reason} /> <span className="quote-age" title={p.price_reason ?? undefined}>{age(p.price_age_seconds)}</span></>}</>}</td>
                <td>{usd(p.stop_price)}</td>
                <td>{usd(p.target_price)}</td>
                <td className={p.unrealized_pnl == null ? '' : p.unrealized_pnl >= 0 ? 'positive' : 'negative'}>{p.unrealized_pnl == null ? <span className="state-tag unpriced" title={p.price_reason ?? undefined}>NOT VALUED</span> : usd(p.unrealized_pnl)}</td>
                <td>{p.r_multiple?.toFixed(2) ?? '—'}</td>
                <td><span className="status-tag quiet">{p.status || 'OPEN'}</span> <StateBadge state={p.price_state} reason={p.price_reason} /></td>
              </tr>)}
              {!positions.length && <Empty span={10} text="There are no open paper positions." />}
            </tbody>
          </table>
        </div>
        {positions.some((p) => p.price_state !== 'FRESH') && <p className="panel-foot valuation-warning">
          {positions.some((p) => p.price_state === 'UNPRICED') ? 'Connect a continuous market-data provider to value this position.' : 'Automatic stop-loss and take-profit are paused until a fresh quote arrives.'} {payload?.market_data_availability_reason ?? ''}
        </p>}
        {positions.length > 0 && payload?.valuation_status === 'PARTIAL' && <p className="panel-foot">{payload.unpriced_position_count} of {positions.length} position(s) have no current price and report no unrealized P&amp;L until a quote arrives.</p>}
      </div>

      <div className="panel health-panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">PORTFOLIO SNAPSHOT</p>
            <h2>Account state</h2>
          </div>
          <span className={`status-tag ${payload?.valuation_status === 'COMPLETE' ? 'good' : 'quiet'}`}>{payload?.valuation_status ?? '—'}</span>
        </div>
        {status
          ? <>
              <Health name="Cash" value={usd(status.portfolio.cash)} />
              <Health name="Equity" value={usd(status.portfolio.equity)} />
              <Health name="Exposure" value={usd(status.portfolio.exposure)} />
              <Health name="Realized P&L" value={usd(status.portfolio.realized_pnl)} />
              <Health name="Unrealized P&L" value={usd(status.portfolio.unrealized_pnl)} />
              <Health name="Valuation" value={status.portfolio.valuation_status} danger={status.portfolio.valuation_status !== 'COMPLETE'} />
              <Health name="Unpriced positions" value={String(status.portfolio.unpriced_position_count)} danger={status.portfolio.unpriced_position_count > 0} />
              <Health name="Pending reservations" value={String(status.portfolio.pending_reservations?.length ?? 0)} />
              <Health name="Market data" value={`${status.market_data_provider} · ${status.market_data_availability}`} danger={status.market_data_availability === 'ERROR'} />
            </>
          : <LoadingState label="Waiting for /api/status…" />}
      </div>
    </section>}
  </>
}
