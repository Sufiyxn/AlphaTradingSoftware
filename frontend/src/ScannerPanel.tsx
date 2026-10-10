import { useCallback, useEffect, useState } from 'react'

type Opportunity = {
  symbol: string
  direction: string
  score: number
  entry_price: number | null
  stop_price: number | null
  target_price: number | null
  reward_risk_ratio: number | null
  reasons: string[]
  valid: boolean
}
type ScannerRow = { symbol: string; error: string | null; eligible: boolean; opportunity: Opportunity | null }
type ScannerPayload = {
  provider: string | null
  provider_state: string | null
  market_session: string
  eligible_count: number
  symbol_count: number
  symbols: ScannerRow[]
}

const POLL_MS = 60000
const usd = (value: number | null) =>
  value == null ? '—' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value)

export default function ScannerPanel({ onSelect }: { onSelect: (symbol: string) => void }) {
  const [payload, setPayload] = useState<ScannerPayload | null>(null)
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    try {
      const response = await fetch('/api/scanner')
      const body = await response.json()
      if (!response.ok) throw new Error(body.error || 'Scanner results are unavailable')
      setPayload(body as ScannerPayload)
      setError('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Scanner results are unavailable')
    }
  }, [])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), POLL_MS)
    return () => window.clearInterval(timer)
  }, [refresh])

  const rows = payload?.symbols ?? []
  return <div className="panel wide-panel">
    <div className="panel-head">
      <div>
        <p className="eyebrow">SCANNER DECISIONS · {payload?.symbol_count ?? 0} SYMBOLS · {payload?.market_session ?? 'UNKNOWN'}</p>
        <h2>Every symbol, with its reason</h2>
      </div>
      <span className="count-badge">{payload?.eligible_count ?? 0} eligible</span>
    </div>
    {error && <p className="negative">{error}</p>}
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Symbol</th><th>Outcome</th><th>Direction</th><th>Entry</th><th>Stop</th><th>Target</th><th>R:R</th><th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const opportunity = row.opportunity
            const outcome = row.error ? 'DATA ERROR' : row.eligible ? 'ELIGIBLE' : 'REJECTED'
            const reason = row.error
              ?? opportunity?.reasons.join('; ')
              ?? 'no strategy evaluation was recorded for this symbol'
            return <tr key={row.symbol} className="scanner-row" onClick={() => onSelect(row.symbol)}>
              <td className="symbol">{row.symbol}</td>
              <td><span className={`status-tag ${row.eligible ? 'good' : 'quiet'}`} title={reason}>{outcome}</span></td>
              <td>{opportunity ? <span className={`direction ${opportunity.direction.toLowerCase()}`}>{opportunity.direction}</span> : '—'}</td>
              <td>{usd(opportunity?.entry_price ?? null)}</td>
              <td>{usd(opportunity?.stop_price ?? null)}</td>
              <td>{usd(opportunity?.target_price ?? null)}</td>
              <td>{opportunity?.reward_risk_ratio?.toFixed(2) ?? '—'}</td>
              <td className="event-message" title={reason}>{reason}</td>
            </tr>
          })}
          {!rows.length && <tr><td colSpan={8} className="empty-state">No scanner rows are available. Connect market data to scan the universe.</td></tr>}
        </tbody>
      </table>
    </div>
    <p className="panel-foot">{payload?.provider ?? '—'} · {payload?.provider_state ?? '—'} · rejections come from the live strategy and risk rules, not from a demo fixture</p>
  </div>
}
