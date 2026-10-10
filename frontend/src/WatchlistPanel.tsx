import { useCallback, useEffect, useState } from 'react'

type WatchlistRow = {
  symbol: string
  state: string
  reason: string | null
  last_price: number | null
  last_price_at: string | null
  previous_close: number | null
  change_percent: number | null
  volume: number | null
  ema: number | null
  rsi: number | null
  atr: number | null
  indicator_state: string
  indicator_reason: string | null
  bar_count: number
  freshness: string | null
  age_seconds: number | null
}
type WatchlistPayload = {
  state: string
  reason: string | null
  provider: string | null
  provider_state: string | null
  timeframe: string
  as_of: string | null
  rows: WatchlistRow[]
}

const POLL_MS = 30000
const usd = (value: number | null) =>
  value == null ? '—' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value)
const num = (value: number | null, digits = 2) =>
  value == null ? '—' : value.toFixed(digits)

export default function WatchlistPanel({ onSelect }: { onSelect: (symbol: string) => void }) {
  const [payload, setPayload] = useState<WatchlistPayload | null>(null)
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    try {
      const response = await fetch('/api/watchlist')
      const body = await response.json()
      if (!response.ok) throw new Error(body.error || 'Watchlist is unavailable')
      setPayload(body as WatchlistPayload)
      setError('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Watchlist is unavailable')
    }
  }, [])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), POLL_MS)
    return () => window.clearInterval(timer)
  }, [refresh])

  const rows = payload?.rows ?? []
  return <div className="panel watchlist-panel">
    <div className="panel-head">
      <div>
        <p className="eyebrow">WATCHLIST · EMA 20 / RSI 14 / ATR 14</p>
        <h2>Universe</h2>
      </div>
      <span className={`status-tag ${payload?.state === 'OK' ? 'good' : 'quiet'}`}>{payload?.state ?? '—'}</span>
    </div>
    {error && <p className="negative">{error}</p>}
    {payload && payload.state === 'NOT_CONFIGURED' && <p className="panel-foot">{payload.reason}</p>}
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Symbol</th><th>Last</th><th>Chg</th><th>EMA</th><th>RSI</th><th>ATR</th><th>Bars</th><th>State</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => <tr key={row.symbol} className="watchlist-row" onClick={() => onSelect(row.symbol)} title={row.reason ?? row.indicator_reason ?? undefined}>
            <td className="symbol">{row.symbol}</td>
            <td>{usd(row.last_price)}</td>
            <td className={row.change_percent == null ? '' : row.change_percent >= 0 ? 'positive' : 'negative'}>
              {row.change_percent == null ? '—' : `${row.change_percent >= 0 ? '+' : ''}${row.change_percent.toFixed(2)}%`}
            </td>
            <td>{num(row.ema)}</td>
            <td>{num(row.rsi)}</td>
            <td>{num(row.atr)}</td>
            <td>{row.bar_count}</td>
            <td>
              <span className={`state-tag ${row.freshness === 'FRESH' ? 'fresh' : row.freshness === 'STALE' ? 'stale' : 'unpriced'}`} title={row.reason ?? row.indicator_reason ?? undefined}>
                {row.state}{row.indicator_state === 'INSUFFICIENT_DATA' ? ' · FEW BARS' : ''}
              </span>
            </td>
          </tr>)}
          {!rows.length && <tr><td colSpan={8} className="empty-state">No watchlist rows are available.</td></tr>}
        </tbody>
      </table>
    </div>
    <p className="panel-foot">{payload?.provider ?? '—'} · {payload?.provider_state ?? '—'} · timeframe {payload?.timeframe ?? '—'} · click a row to chart it</p>
  </div>
}
