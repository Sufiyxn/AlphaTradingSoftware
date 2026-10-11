import { useCallback, useEffect, useRef, useState } from 'react'
import type { PanelLoad } from './types'

type Decision = {
  cycle: number
  timestamp: string
  symbol: string | null
  stage: string
  outcome: string
  reasons: string[]
  context: Record<string, unknown> | null
}
export type DecisionsPayload = {
  summary: {
    total: number
    approved: number
    rejected: number
    skipped: number
    by_stage: Record<string, number>
    latest_cycle: number
    latest: Decision | null
  }
  decisions: Decision[]
}

const POLL_MS = 15000
const time = (value: string) => new Date(value).toLocaleTimeString()

export default function EnginePanel({ onLoad }: { onLoad?: (result: PanelLoad<DecisionsPayload>) => void } = {}) {
  const [payload, setPayload] = useState<DecisionsPayload | null>(null)
  const [error, setError] = useState('')
  const onLoadRef = useRef(onLoad)
  useEffect(() => { onLoadRef.current = onLoad })

  const refresh = useCallback(async () => {
    try {
      const response = await fetch('/api/decisions?limit=50')
      const body = await response.json()
      if (!response.ok) throw new Error(body.error || 'Decision records are unavailable')
      setPayload(body as DecisionsPayload)
      setError('')
      onLoadRef.current?.({ data: body as DecisionsPayload, error: '' })
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : 'Decision records are unavailable'
      setError(message)
      onLoadRef.current?.({ data: null, error: message })
    }
  }, [])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), POLL_MS)
    return () => window.clearInterval(timer)
  }, [refresh])

  const summary = payload?.summary
  const decisions = [...(payload?.decisions ?? [])].reverse()
  return <>
    <div className="panel-head">
      <div>
        <p className="eyebrow">DECISION ENGINE</p>
        <h2>Cycle {summary?.latest_cycle ?? 0} decisions</h2>
      </div>
      <span className={`status-tag ${summary && summary.approved > 0 ? 'good' : 'quiet'}`}>{summary?.total ?? 0} recorded</span>
    </div>
    {error && <p className="negative">{error}</p>}
    <div className="engine-stats">
      <span>Approved <b className="positive">{summary?.approved ?? 0}</b></span>
      <span>Rejected <b className="negative">{summary?.rejected ?? 0}</b></span>
      <span>Skipped <b>{summary?.skipped ?? 0}</b></span>
    </div>
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>Time</th><th>Cycle</th><th>Symbol</th><th>Stage</th><th>Outcome</th><th>Reason</th></tr>
        </thead>
        <tbody>
          {decisions.map((decision, index) => <tr key={`${decision.cycle}-${index}`}>
            <td>{time(decision.timestamp)}</td>
            <td>{decision.cycle}</td>
            <td className="symbol">{decision.symbol ?? '—'}</td>
            <td><span className="status-tag quiet">{decision.stage}</span></td>
            <td className={decision.outcome === 'APPROVED' ? 'positive' : decision.outcome === 'REJECTED' ? 'negative' : ''}>{decision.outcome}</td>
            <td className="event-message" title={decision.reasons.join('; ')}>{decision.reasons.join('; ') || '—'}</td>
          </tr>)}
          {!decisions.length && <tr><td colSpan={6} className="empty-state">No decisions have been recorded yet this session. Every candidate will leave a record here once the engine runs.</td></tr>}
        </tbody>
      </table>
    </div>
    <p className="panel-foot">Every stage records its own decision · the ledger rebuilds from the journal after a restart</p>
  </>
}
