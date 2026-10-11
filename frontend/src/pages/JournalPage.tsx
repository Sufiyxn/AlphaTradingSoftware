/**
 * #/journal — the trade journal: closed trades, the filtered event log with
 * its JSON/CSV exports, the measured-performance panel, and the existing
 * reports and backtest tools.
 */
import { useState } from 'react'
import { useApi } from '../useApi'
import { Empty, ErrorState, Health, LoadingState, Metric, time, usd } from '../components'
import type { PageProps } from '../components'
import BacktestPanel from '../BacktestPanel'
import ReportsPanel from '../ReportsPanel'
import type { ActivityPayload, Log, Performance, TradesPayload } from '../types'

export default function JournalPage({ status, refresh }: PageProps) {
  const tradesApi = useApi<TradesPayload>('/api/trades', { trigger: refresh })
  const performanceApi = useApi<Performance>('/api/performance', { trigger: refresh })
  const activityApi = useApi<ActivityPayload>('/api/activity?limit=50', { trigger: refresh })
  const [logQuery, setLogQuery] = useState('')
  const [logSymbol, setLogSymbol] = useState('')
  const [logStart, setLogStart] = useState('')
  const [logEnd, setLogEnd] = useState('')

  const trades = tradesApi.data?.trades ?? []
  const performance = performanceApi.data
  const exportQuery = new URLSearchParams({ kind: 'all', format: 'json' })
  if (logSymbol) exportQuery.set('symbol', logSymbol)
  if (logStart) exportQuery.set('start', logStart)
  if (logEnd) exportQuery.set('end', logEnd)
  const csvQuery = new URLSearchParams(exportQuery)
  csvQuery.set('format', 'csv')

  const entries: Log[] = [...trades.slice(0, 10).map((trade): Log => ({
    record_type: 'trade',
    timestamp: trade.exit_timestamp || trade.entry_timestamp,
    symbol: trade.symbol,
    message: `${trade.direction} ${trade.quantity} · ${trade.strategy}`,
    pnl: trade.pnl,
  })), ...(activityApi.data?.events ?? [])]
    .filter((row) =>
      `${row.record_type} ${row.symbol ?? ''} ${row.message ?? ''} ${row.event_type ?? ''}`.toLowerCase().includes(logQuery.toLowerCase())
      && (!logSymbol || row.symbol?.toUpperCase().includes(logSymbol.trim().toUpperCase()))
      && (!logStart || (row.timestamp || row.entry_timestamp || '').slice(0, 10) >= logStart)
      && (!logEnd || (row.timestamp || row.entry_timestamp || '').slice(0, 10) <= logEnd))
    .slice(0, 50)

  return <>
    {tradesApi.phase === 'loading' && <LoadingState label="Loading the trade journal…" />}
    {tradesApi.phase === 'error' && <ErrorState what="Trade journal" message={tradesApi.error} />}
    {tradesApi.phase === 'ready' && <section className="panel extra-section">
      <div className="panel-head">
        <div>
          <p className="eyebrow">JOURNAL TRADES</p>
          <h2>Closed and open trades</h2>
        </div>
        <span className="count-badge">{trades.length}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Trade</th><th>Symbol</th><th>Side</th><th>Strategy</th><th>Qty</th><th>Entry</th><th>Entry time</th><th>Exit</th><th>Exit time</th><th>P&amp;L</th><th>State</th></tr>
          </thead>
          <tbody>
            {trades.map((trade) => <tr key={trade.trade_id}>
              <td className="symbol">{trade.trade_id}</td>
              <td className="symbol">{trade.symbol}</td>
              <td><span className={`direction ${trade.direction.toLowerCase()}`}>{trade.direction}</span></td>
              <td>{trade.strategy}</td>
              <td>{trade.quantity}</td>
              <td>{usd(trade.entry_price)}</td>
              <td>{time(trade.entry_timestamp)}</td>
              <td>{trade.exit_price == null ? '—' : usd(trade.exit_price)}</td>
              <td>{time(trade.exit_timestamp)}</td>
              <td className={trade.pnl == null ? '' : trade.pnl >= 0 ? 'positive' : 'negative'}>{trade.pnl == null ? <span className="state-tag unpriced">NOT CLOSED</span> : usd(trade.pnl)}</td>
              <td><span className={`status-tag ${trade.exit_timestamp ? 'quiet' : 'good'}`}>{trade.exit_timestamp ? 'CLOSED' : 'OPEN'}</span></td>
            </tr>)}
            {!trades.length && <Empty span={11} text="No trades have been recorded in the journal yet." />}
          </tbody>
        </table>
      </div>
      <p className="panel-foot">Every trade is read from the local journal — open trades show no P&amp;L until the paper adapter records an exit.</p>
    </section>}

    <section className="panel recent-panel">
      <div className="panel-head">
        <div>
          <p className="eyebrow">PERSISTENT JOURNAL</p>
          <h2>Recent trades and events</h2>
        </div>
        <div className="log-tools">
          <input className="log-filter" aria-label="Search recent logs" placeholder="Search event" value={logQuery} onChange={(e) => setLogQuery(e.target.value)} />
          <input className="log-filter" aria-label="Filter log symbol" placeholder="Symbol" value={logSymbol} onChange={(e) => setLogSymbol(e.target.value)} />
          <input className="log-filter" aria-label="Log start date" type="date" value={logStart} onChange={(e) => setLogStart(e.target.value)} />
          <input className="log-filter" aria-label="Log end date" type="date" value={logEnd} onChange={(e) => setLogEnd(e.target.value)} />
          <a className="text-link" href={`/api/logs?${exportQuery.toString()}`} target="_blank" rel="noreferrer">JSON ↗</a>
          <a className="text-link" href={`/api/logs?${csvQuery.toString()}`} target="_blank" rel="noreferrer">CSV</a>
        </div>
      </div>
      {activityApi.phase === 'loading' && <LoadingState label="Loading journal events…" />}
      {activityApi.phase === 'error' && <ErrorState what="Journal events" message={activityApi.error} />}
      {activityApi.phase === 'ready' && <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Type</th><th>Time</th><th>Symbol</th><th>Details</th><th>Result</th></tr>
          </thead>
          <tbody>
            {entries.map((row, i) => <tr key={`${row.record_type}-${i}`}>
              <td><span className="status-tag quiet">{row.record_type.toUpperCase()}</span></td>
              <td>{time(row.timestamp || row.entry_timestamp)}</td>
              <td className="symbol">{row.symbol ?? '—'}</td>
              <td className="event-message">{row.message ?? row.event_type ?? 'Journal event'}</td>
              <td className={(row.pnl || 0) >= 0 ? 'positive' : 'negative'}>{row.pnl == null ? '—' : usd(row.pnl)}</td>
            </tr>)}
            {!entries.length && <Empty span={5} text="No matching recent journal events." />}
          </tbody>
        </table>
      </div>}
    </section>

    {performanceApi.phase === 'loading' && <LoadingState label="Loading performance metrics…" />}
    {performanceApi.phase === 'error' && <ErrorState what="Performance metrics" message={performanceApi.error} />}
    {performanceApi.phase === 'ready' && <>
      <section className="metric-grid">
        <Metric label="Closed trades" value={String(performance?.total_trades ?? 0)} note="All time" />
        <Metric label="Win rate" value={`${(performance?.win_rate ?? 0).toFixed(1)}%`} note={`${performance?.daily_trades ?? 0} closed today`} />
        <Metric label="Realized P&L" value={usd(performance?.realized_pnl)} note={`Today: ${usd(performance?.daily_realized_pnl)}`} tone={(performance?.realized_pnl ?? 0) >= 0 ? 'positive' : 'negative'} />
        <Metric label="Evaluation window" value={`${(performance?.evaluation_win_rate ?? 0).toFixed(1)}%`} note={`${performance?.qualification_status ?? 'NOT_QUALIFIED'} · ${performance?.evaluation_trades ?? 0} trades`} />
      </section>
      <section className="panel metrics-detail">
        <div className="panel-head">
          <div>
            <p className="eyebrow">PERFORMANCE</p>
            <h2>Measured results</h2>
          </div>
          <span className={`status-tag ${performance?.qualification_status === 'QUALIFIED' ? 'good' : 'quiet'}`}>{performance?.qualification_status ?? 'NOT_QUALIFIED'}</span>
        </div>
        <div className="performance-grid">
          <Health name="Closed trades" value={String(performance?.total_trades ?? 0)} />
          <Health name="Win rate" value={`${(performance?.win_rate ?? 0).toFixed(1)}%`} />
          <Health name="Average win / loss" value={`${usd(performance?.average_win)} / ${usd(performance?.average_loss)}`} />
          <Health name="Profit factor" value={performance?.profit_factor?.toFixed(2) ?? '—'} />
          <Health name="Max drawdown" value={usd(performance?.maximum_drawdown)} />
          <Health name={`Unrealized P&L${status?.valuation_status === 'PARTIAL' ? ' (valued positions)' : ''}`} value={usd(performance?.unrealized_pnl)} />
        </div>
      </section>
    </>}

    <section className="panel extra-section">
      <ReportsPanel />
    </section>
    <section className="panel extra-section">
      <BacktestPanel />
    </section>
  </>
}
