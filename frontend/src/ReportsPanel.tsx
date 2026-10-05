import { useEffect, useState } from 'react'

type ReportRow = { report_date: string; context_json: { status?: string; trades?: number; wins?: number; losses?: number; daily_win_rate?: number; realized_pnl?: number; qualification_status?: string } }
const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}` }
export default function ReportsPanel() {
  const [day, setDay] = useState(today)
  const [reports, setReports] = useState<ReportRow[]>([])
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const history = async () => { const response = await fetch('/api/reports'); if (response.ok) setReports((await response.json()).reports ?? []) }
  useEffect(() => { void history() }, [])
  const generate = async () => {
    setBusy(true); setMessage('')
    try {
      const response = await fetch(`/api/reports?date=${encodeURIComponent(day)}`)
      const result = await response.json()
      if (!response.ok) throw new Error(result.error || 'Report generation failed')
      setMessage(`${result.status ?? result.qualification_status ?? 'Report saved'} · ${result.trades ?? 0} closed trades · ${Number(result.daily_win_rate ?? 0).toFixed(1)}% daily win rate`)
      await history()
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : 'Report generation failed') }
    finally { setBusy(false) }
  }
  return <><div className="panel-head"><div><p className="eyebrow">REPORTING</p><h2>Daily reports</h2></div><div className="export-links"><a className="text-link" href="/api/logs?kind=all&amp;format=json" target="_blank" rel="noreferrer">JSON export</a><a className="text-link" href="/api/logs?kind=all&amp;format=csv" target="_blank" rel="noreferrer">CSV export</a></div></div>
    <div className="report-actions"><label>Report date <input type="date" value={day} onChange={(e) => setDay(e.target.value)} /></label><button className="button" disabled={busy || !day} onClick={() => void generate()}>{busy ? 'Generating…' : 'Generate on demand'}</button></div>{message && <p className="report-message">{message}</p>}
    <div className="table-wrap"><table><thead><tr><th>Date</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Daily win rate</th><th>Realized P&amp;L</th><th>Qualification</th></tr></thead><tbody>{reports.map((row) => <tr key={row.report_date}><td>{row.report_date}</td><td>{row.context_json.trades ?? 0}</td><td>{row.context_json.wins ?? 0}</td><td>{row.context_json.losses ?? 0}</td><td>{(row.context_json.daily_win_rate ?? 0).toFixed(1)}%</td><td>{(row.context_json.realized_pnl ?? 0).toFixed(2)}</td><td>{row.context_json.qualification_status ?? 'NOT_QUALIFIED'}</td></tr>)}{!reports.length && <tr><td className="empty-state" colSpan={7}>No daily reports saved yet.</td></tr>}</tbody></table></div>
  </>
}
