/**
 * #/overview — the summary page: headline metrics, the runtime/system-health
 * panel and a short recent-activity digest. Detailed tables live on their
 * own pages; this one stays a summary.
 */
import { useApi } from '../useApi'
import { EmptyState, ErrorState, Health, LoadingState, Metric, time, usd } from '../components'
import type { PageProps } from '../components'
import type { ActivityPayload, Performance } from '../types'

export default function OverviewPage({ status, statusPhase, statusError, refresh }: PageProps) {
  const performanceApi = useApi<Performance>('/api/performance', { trigger: refresh })
  const activityApi = useApi<ActivityPayload>('/api/activity?limit=8', { trigger: refresh, pollMs: 15000 })

  const account = status?.portfolio
  const performance = performanceApi.data
  const events = activityApi.data?.events ?? []

  return <>
    <section className="metric-grid">
      <Metric label="Portfolio equity" value={usd(account?.equity)} note="Paper account" />
      <Metric
        label="Today's P&L"
        value={usd(performance?.daily_realized_pnl)}
        note={`${performance?.daily_trades ?? 0} closed today`}
        tone={(performance?.daily_realized_pnl || 0) >= 0 ? 'positive' : 'negative'}
      />
      <Metric label="Open exposure" value={usd(account?.exposure)} note={`${status?.orchestrator.active_positions ?? 0} active positions`} />
      <Metric
        label="Evaluation win rate"
        value={`${(performance?.evaluation_win_rate ?? 0).toFixed(1)}%`}
        note={`${performance?.qualification_status ?? 'NOT_QUALIFIED'} · ${performance?.evaluation_trades ?? 0} trades`}
      />
    </section>
    {performanceApi.phase === 'error' && <p className="negative">Performance metrics unavailable: {performanceApi.error}.</p>}

    <section className="page-two-col">
      <div className="panel recent-panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">RECENT ACTIVITY</p>
            <h2>Latest journal events</h2>
          </div>
          <a className="text-link" href="#/journal">Open full journal →</a>
        </div>
        {activityApi.phase === 'loading' && <LoadingState label="Loading recent activity…" />}
        {activityApi.phase === 'error' && <ErrorState what="Recent activity" message={activityApi.error} />}
        {activityApi.phase === 'ready' && (events.length
          ? <div className="activity-list">
              {events.slice(0, 6).map((row, index) => (
                <div className="activity-row" key={`${row.record_type}-${row.timestamp ?? ''}-${index}`}>
                  <span className="activity-time">{time(row.timestamp || row.entry_timestamp)}</span>
                  <span className={`status-tag ${row.record_type === 'error' ? 'quiet negative' : 'quiet'}`}>{row.record_type.toUpperCase()}</span>
                  <span className="symbol">{row.symbol ?? '—'}</span>
                  <span className="event-message" title={row.message ?? row.event_type ?? undefined}>{row.message ?? row.event_type ?? 'Journal event'}</span>
                </div>
              ))}
            </div>
          : <EmptyState message="No journal events have been recorded yet. Events appear here once the backend records a cycle, signal or order." />)}
        {activityApi.phase === 'ready' && events.length > 6 && <p className="panel-foot">Showing the 6 newest of {events.length} loaded events · older events live in the journal.</p>}
      </div>

      <div className="panel health-panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">SYSTEM HEALTH</p>
            <h2>Runtime</h2>
          </div>
          <span className="pulse-dot" />
        </div>
        {statusPhase === 'loading' && <LoadingState label="Loading runtime status…" />}
        {statusPhase === 'error' && <ErrorState what="Runtime status" message={statusError} />}
        {status && <>
          <Health name="Market data" value={`${status.market_data_provider} · ${status.market_data_availability}`} danger={status.market_data_availability === 'ERROR'} />
          <Health name="Execution adapter" value={status.execution_adapter} />
          <Health name="Market session" value={`${status.market_session} · ${status.market_session_policy} policy · ${status.market_session_orders_allowed ? 'orders allowed' : 'orders blocked'}`} />
          <Health name="Mode" value="PAPER ONLY" />
          <Health name="Kill switch" value={status.risk_kill_switch ? 'ENGAGED' : 'READY'} danger={status.risk_kill_switch} />
          <Health name="Last cycle" value={time(status.orchestrator.last_scan_timestamp)} />
          <Health name="Decision engine" value={`${status.orchestrator.decisions_total ?? 0} recorded · ${status.orchestrator.last_decision ? `${status.orchestrator.last_decision.stage} ${status.orchestrator.last_decision.outcome}` : 'no decisions yet'}`} />
          <Health name="Stale-data rejections" value={String(status.orchestrator.data_stale_rejected ?? 0)} />
          <Health name="Interrupted cycles" value={String(status.orchestrator.interrupted_cycles ?? 0)} />
        </>}
      </div>
    </section>
  </>
}
