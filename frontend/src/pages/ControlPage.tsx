/**
 * #/control — the restricted owner control center. Every action posts with
 * the X-Owner-Token header; when the backend reports owner controls
 * disabled (status flag or the 503 refusal) the page explains the refusal
 * instead of showing dead buttons.
 */
import { useState } from 'react'
import { ErrorState, Health, LoadingState } from '../components'
import type { PageProps } from '../components'
import { ownerControlsAvailability } from '../routes'
import { postControl } from '../useApi'

export default function ControlPage({ status, statusPhase, statusError, token, onTokenChange, refreshShell }: PageProps) {
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const controls = ownerControlsAvailability(status?.owner_controls_enabled, notice)
  const running = status?.orchestrator.status === 'RUNNING'

  const control = async (action: string, body: Record<string, unknown> = {}) => {
    if (!token) { setNotice('Enter the owner control token configured in backend .env.'); return }
    setBusy(true)
    setNotice('')
    try {
      setNotice(await postControl(action, token, body))
      refreshShell()
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : 'Control request failed')
    } finally {
      setBusy(false)
    }
  }

  return <section className="page-two-col">
    <div className="panel controls-panel">
      <div className="panel-head">
        <div>
          <p className="eyebrow">OWNER ACTIONS · RESTRICTED</p>
          <h2>Bot controls</h2>
        </div>
        <span className={`status-tag ${running ? 'good' : 'quiet'}`}>{status?.orchestrator.status ?? 'OFFLINE'}</span>
      </div>
      {statusPhase === 'loading' && <LoadingState label="Loading owner-control state…" />}
      {statusPhase === 'error' && <ErrorState what="Owner controls" message={statusError} />}
      {statusPhase === 'ready' && <>
        <label className="field-label" htmlFor="owner-token">Owner token</label>
        <input id="owner-token" type="password" value={token} onChange={(e) => onTokenChange(e.target.value)} placeholder="Set OWNER_CONTROL_TOKEN" autoComplete="off" />
        {!controls.available
          ? <div className="alert danger">{controls.reason} The control buttons stay hidden until the backend reports owner controls enabled — reload this page after fixing the configuration.</div>
          : <>
              <div className="button-grid">
                <button disabled={busy || running} onClick={() => void control('start')}>Start</button>
                <button disabled={busy || !running} onClick={() => void control('pause')}>Pause</button>
                <button disabled={busy || running} onClick={() => void control('resume')}>Resume</button>
                <button disabled={busy} onClick={() => void control('stop')}>Stop</button>
                <button className="danger-button" disabled={busy} onClick={() => { const reason = window.prompt('Reason for emergency stop'); if (reason) void control('emergency-stop', { reason }) }}>Emergency stop</button>
                <button className="danger-button" disabled={busy} onClick={() => { if (window.confirm('Safely terminate the local application?')) void control('safe-termination') }}>Terminate</button>
              </div>
              {status?.risk_kill_switch && <button className="reset-button" disabled={busy} onClick={() => { const reason = window.prompt('Enter the reviewed reset reason'); if (reason) void control('reset-emergency-stop', { reason }) }}>Reset emergency stop</button>}
            </>}
        {notice && <div className="alert">{notice}</div>}
        <div className="control-foot">
          <span>Cycle <b>{status?.orchestrator.current_cycle ?? 0}</b></span>
          <span>Risk rejections <b>{status?.orchestrator.trades_rejected_by_risk ?? 0}</b></span>
        </div>
        <div className="safety-note">Live trading is unavailable. All order paths use the local paper adapter.</div>
      </>}
    </div>

    <div className="panel health-panel">
      <div className="panel-head">
        <div>
          <p className="eyebrow">RISK &amp; KILL-SWITCH STATE</p>
          <h2>Guard rails</h2>
        </div>
        <span className={`status-tag ${status?.risk_kill_switch ? 'quiet negative' : 'good'}`}>{status?.risk_kill_switch ? 'ENGAGED' : 'READY'}</span>
      </div>
      {statusPhase === 'loading' && <LoadingState label="Loading risk state…" />}
      {statusPhase === 'error' && <ErrorState what="Risk state" message={statusError} />}
      {status && <>
        <Health name="Kill switch" value={status.risk_kill_switch ? 'ENGAGED — entries are blocked until a reviewed reset' : 'READY'} danger={status.risk_kill_switch} />
        <Health name="Orchestrator" value={status.orchestrator.status} />
        <Health name="Current cycle" value={String(status.orchestrator.current_cycle)} />
        <Health name="Trades rejected by risk" value={String(status.orchestrator.trades_rejected_by_risk)} />
        <Health name="Candidates rejected" value={String(status.orchestrator.candidates_rejected)} />
        <Health name="Stale-data rejections" value={String(status.orchestrator.data_stale_rejected ?? 0)} />
        <Health name="Market session" value={`${status.market_session} · ${status.market_session_orders_allowed ? 'orders allowed' : 'orders blocked'}`} />
        <Health name="Owner controls" value={controls.available ? 'Enabled' : 'Disabled'} danger={!controls.available} />
        <p className="panel-foot">Session blocked: {status.orchestrator.session_blocked ? (status.orchestrator.session_reason ?? 'Yes') : 'No'} · paper mode only, live execution is locked in this build.</p>
      </>}
    </div>
  </section>
}
