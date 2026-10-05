import { useState } from 'react'

type Result = { trades: { symbol: string; won: boolean; pnl: number; exit_reason: string }[]; metrics: { total_trades: number; win_rate: number; total_pnl: number; maximum_drawdown: number }; qualified: boolean; completed: boolean; errors: string[] }
const money = (value: number) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value)
export default function BacktestPanel() {
  const [result, setResult] = useState<Result | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const run = async () => {
    setBusy(true); setError('')
    try { const response = await fetch('/api/backtest'); const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Backtest failed'); setResult(data) }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Backtest failed') }
    finally { setBusy(false) }
  }
  return <><div className="panel-head"><div><p className="eyebrow">BACKTEST</p><h2>Local baseline run</h2></div><button className="button" disabled={busy} onClick={() => void run()}>{busy ? 'Running…' : 'Run backtest'}</button></div>
    <p className="readiness-copy">Uses only the deterministic synthetic mock dataset and existing baseline strategy. Results are a software check, not evidence of strategy profitability. The 51% qualification rule is applied by the backtest engine.</p>
    {error && <p className="negative">{error}</p>}{result && <><div className="backtest-metrics"><span>Trades <b>{result.metrics.total_trades}</b></span><span>Win rate <b>{result.metrics.win_rate.toFixed(1)}%</b></span><span>Net P&amp;L <b>{money(result.metrics.total_pnl)}</b></span><span>Max drawdown <b>{money(result.metrics.maximum_drawdown)}</b></span><span>Qualification <b>{result.qualified ? 'QUALIFIED' : 'NOT_QUALIFIED'}</b></span><span>Run <b>{result.completed ? 'COMPLETE' : 'INCOMPLETE'}</b></span></div>{result.errors.length > 0 && <details><summary>Run issues ({result.errors.length})</summary><ul>{result.errors.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}<div className="table-wrap"><table><thead><tr><th>Symbol</th><th>Result</th><th>P&amp;L</th><th>Exit</th></tr></thead><tbody>{result.trades.map((trade, index) => <tr key={`${trade.symbol}-${index}`}><td>{trade.symbol}</td><td>{trade.won ? 'Win' : 'Loss'}</td><td>{money(trade.pnl)}</td><td>{trade.exit_reason}</td></tr>)}{!result.trades.length && <tr><td colSpan={4} className="empty-state">No completed trades in this mock-data run.</td></tr>}</tbody></table></div></>}
  </>
}
