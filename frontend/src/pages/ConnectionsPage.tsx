/**
 * #/connections — the connections panel plus the safety settings form and
 * the configuration links, all against the shared owner token. When the
 * backend reports owner controls disabled, the settings actions explain
 * themselves instead of failing silently.
 */
import { useState } from 'react'
import { ErrorState, LoadingState, Setting } from '../components'
import type { PageProps } from '../components'
import ConnectionsPanel from '../ConnectionsPanel'
import type { ConnectionsSnapshot } from '../ConnectionsPanel'
import { classifyApiPhase, ownerControlsAvailability } from '../routes'
import { postControl } from '../useApi'
import type { PanelLoad } from '../types'

export default function ConnectionsPage({ status, statusPhase, token, onTokenChange, refreshShell, refresh }: PageProps) {
  const [panel, setPanel] = useState<PanelLoad<ConnectionsSnapshot>>({ data: null, error: '' })
  const [notice, setNotice] = useState('')
  const [scanSeconds, setScanSeconds] = useState('30')
  const [lookbackDays, setLookbackDays] = useState('90')
  const [priceLimit, setPriceLimit] = useState('100')
  const phase = classifyApiPhase(panel.data !== null, panel.error !== '')
  const controls = ownerControlsAvailability(status?.owner_controls_enabled, notice)

  const control = async (action: string, body: Record<string, unknown> = {}) => {
    if (!token) { setNotice('Enter the owner control token configured in backend .env.'); return }
    setNotice('')
    try {
      setNotice(await postControl(action, token, body))
      refreshShell()
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : 'Control request failed')
    }
  }

  // The panel owns the fetch that reports back through `onLoad`, so it has to
  // be mounted while the page is still `loading` - gating it on `ready` waits
  // for data only that panel can request. It stays hidden (same pattern as
  // Watchlists) until its first payload arrives, so an empty card is never
  // shown as if it were the answer.
  return <>
    {phase === 'loading' && <LoadingState label="Loading connection records…" />}
    {phase === 'error' && <ErrorState what="Connections" message={panel.error} />}
    <div hidden={phase !== 'ready'}>
      <section className="panel extra-section">
        <ConnectionsPanel
          key={refresh}
          token={token}
          onLoad={(result) => setPanel((previous) => ({ data: result.data ?? previous.data, error: result.error }))}
        />
      </section>
    </div>

    <section className="panel extra-section">
      <div className="panel-head">
        <div>
          <p className="eyebrow">SETTINGS</p>
          <h2>Safety settings</h2>
        </div>
        <span className={`status-tag ${status?.owner_controls_enabled ? 'good' : 'quiet'}`}>{statusPhase === 'ready' ? (status?.owner_controls_enabled ? 'OWNER CONTROLS READY' : 'OWNER CONTROLS DISABLED') : 'CHECKING'}</span>
      </div>
      <p className="readiness-copy">Candidate entries at $100 or higher are rejected. The 2:1 reward/risk and 51% evaluation rules are fixed safety requirements.</p>
      {statusPhase === 'ready' && !controls.available && controls.reason && <div className="alert danger">{controls.reason} Connection tests below also require this token.</div>}
      <label className="field-label" htmlFor="connections-owner-token">Owner token</label>
      <input id="connections-owner-token" type="password" value={token} onChange={(e) => onTokenChange(e.target.value)} placeholder="Set OWNER_CONTROL_TOKEN" autoComplete="off" />
      <div className="settings-form">
        <Setting label="Scan interval (seconds)" value={scanSeconds} onChange={setScanSeconds} onSave={() => void control('configuration', { key: 'scan_interval_seconds', value: Number(scanSeconds) })} />
        <Setting label="History lookback (days)" value={lookbackDays} onChange={setLookbackDays} onSave={() => void control('configuration', { key: 'history_lookback_days', value: Number(lookbackDays) })} />
        <Setting label="Entry price ceiling (max $100)" value={priceLimit} onChange={setPriceLimit} onSave={() => void control('configuration', { key: 'max_candidate_price', value: Number(priceLimit) })} />
      </div>
      {notice && <div className="alert">{notice}</div>}
      <p className="readiness-copy">Configuration links (read-only JSON served by the local backend):</p>
      <div className="log-tools">
        <a className="text-link" href="/api/configuration" target="_blank" rel="noreferrer">Safe runtime configuration ↗</a>
        <a className="text-link" href="/api/readiness" target="_blank" rel="noreferrer">Live readiness checks ↗</a>
        <a className="text-link" href="/api/connections" target="_blank" rel="noreferrer">Connection state ↗</a>
        <a className="text-link" href="#/control">Owner control center →</a>
      </div>
      <div className="safety-note">Live trading is unavailable. All order paths use the local paper adapter, and secret values are never returned to this screen.</div>
    </section>
  </>
}
