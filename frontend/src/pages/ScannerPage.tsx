/**
 * #/scanner — the scanner panel plus strategy context: the fixed safety
 * rules and a tally of the rejection reasons the backend itself reported
 * through /api/scanner. Nothing here is recomputed into prices or signals.
 */
import { useState } from 'react'
import { EmptyState, ErrorState, LoadingState } from '../components'
import type { PageProps } from '../components'
import EnginePanel from '../EnginePanel'
import ScannerPanel from '../ScannerPanel'
import { classifyApiPhase } from '../routes'
import type { DecisionsPayload } from '../EnginePanel'
import type { ScannerPayload } from '../ScannerPanel'
import type { PanelLoad } from '../types'

type ScannerSummary = {
  total: number
  eligible: number
  dataErrors: number
  strategyRejections: number
  reasons: [string, number][]
}

/** Tally the rows /api/scanner returned; every count comes from a real row. */
export function summarizeScanner(payload: ScannerPayload | null): ScannerSummary {
  const rows = payload?.symbols ?? []
  const reasons = new Map<string, number>()
  let eligible = 0
  let dataErrors = 0
  let strategyRejections = 0
  for (const row of rows) {
    if (row.eligible) {
      eligible += 1
      continue
    }
    if (row.error) {
      dataErrors += 1
      reasons.set(row.error, (reasons.get(row.error) ?? 0) + 1)
      continue
    }
    strategyRejections += 1
    const list = row.opportunity?.reasons ?? []
    if (!list.length) {
      const fallback = 'no strategy evaluation was recorded for this symbol'
      reasons.set(fallback, (reasons.get(fallback) ?? 0) + 1)
    }
    for (const reason of list) {
      reasons.set(reason, (reasons.get(reason) ?? 0) + 1)
    }
  }
  return {
    total: rows.length,
    eligible,
    dataErrors,
    strategyRejections,
    reasons: [...reasons.entries()].sort((a, b) => (b[1] - a[1] || a[0].localeCompare(b[0]))),
  }
}

export default function ScannerPage({ onChartSymbolChange, refresh }: PageProps) {
  const [scanner, setScanner] = useState<PanelLoad<ScannerPayload>>({ data: null, error: '' })
  const [engine, setEngine] = useState<PanelLoad<DecisionsPayload>>({ data: null, error: '' })
  const scannerPhase = classifyApiPhase(scanner.data !== null, scanner.error !== '')
  const enginePhase = classifyApiPhase(engine.data !== null, engine.error !== '')
  const summary = summarizeScanner(scanner.data)

  return <>
    <section className="page-two-col">
      <div>
        {scannerPhase === 'loading' && <LoadingState label="Loading scanner decisions…" />}
        {scannerPhase === 'error' && <ErrorState what="Scanner" message={scanner.error} />}
        {/* Mounted-but-hidden until the panel reports its first payload, so it can fetch. */}
        <div hidden={scannerPhase !== 'ready'}>
          <ScannerPanel
            key={refresh}
            onSelect={onChartSymbolChange}
            onLoad={(result) => setScanner((previous) => ({ data: result.data ?? previous.data, error: result.error }))}
          />
        </div>
      </div>
      <div className="panel health-panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">STRATEGY CONTEXT</p>
            <h2>Why a symbol was skipped</h2>
          </div>
          <span className="count-badge">{summary.total} scanned</span>
        </div>
        <p className="readiness-copy">Candidate entries at $100 or higher are rejected. The 2:1 reward/risk and 51% evaluation rules are fixed safety requirements — no page or control can relax them.</p>
        {scannerPhase === 'loading' && <LoadingState label="Waiting for scanner decisions…" />}
        {scannerPhase === 'error' && <ErrorState what="Strategy context" message={scanner.error} />}
        {scannerPhase === 'ready' && (summary.total === 0
          ? <EmptyState message="The scanner has not evaluated any symbol yet. Connect market data, then the universe appears here with a reason per symbol." />
          : <>
              <div className="engine-stats">
                <span>Eligible <b className="positive">{summary.eligible}</b></span>
                <span>Rejected <b className="negative">{summary.strategyRejections}</b></span>
                <span>Data errors <b>{summary.dataErrors}</b></span>
              </div>
              <p className="field-label">Rejection reasons reported by /api/scanner</p>
              <div className="reason-tally">
                {summary.reasons.map(([reason, count]) => <div key={reason}><span className="event-message" title={reason}>{reason}</span><b>×{count}</b></div>)}
                {!summary.reasons.length && <p className="panel-foot">Every scanned symbol was eligible; no rejection reasons were recorded.</p>}
              </div>
            </>)}
      </div>
    </section>

    <section className="panel extra-section engine-panel" hidden={enginePhase !== 'ready'}>
      {enginePhase === 'loading' && <LoadingState label="Loading decision-engine records…" />}
      {enginePhase === 'error' && <ErrorState what="Decision engine" message={engine.error} />}
      <EnginePanel key={refresh} onLoad={(result) => setEngine((previous) => ({ data: result.data ?? previous.data, error: result.error }))} />
    </section>
  </>
}
