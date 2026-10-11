import { useEffect, useRef, useState } from 'react'
import type { PanelLoad } from './types'

type TestStatus = { status: string; tested_at: string | null; error?: string | null; message?: string }
type Section = {
  provider?: string; mode?: string; configured?: boolean; base_url_configured?: boolean
  plan_configured?: boolean; name?: string; adapter?: string; environment?: string
  account_id_configured?: boolean; live_execution_available?: boolean; model?: string
  endpoint_configured?: boolean; enabled?: boolean; role?: string; destination_configured?: boolean
  last_successful_connection?: string | null
  webhook_available?: boolean; secret_configured?: boolean; endpoint_status?: string
  last_received_signal?: string | null; connection?: TestStatus; account_connection?: TestStatus
  capabilities?: unknown
  endpoint?: string; ready?: boolean; state?: string; reason?: string | null
  signals_accepted?: number; signals_rejected?: number; signals_duplicate?: number
  allowed_strategies?: string[]; max_signal_age_seconds?: number; stale_after_seconds?: number
  label?: string; implemented?: boolean; missing_credentials?: string[]
  connectivity?: {
    state?: string | null; reason?: string | null; checked_at?: string | null
    last_success_at?: string | null; last_error?: string | null
    rate_limited_until?: string | null
  }
  count?: number; ready_count?: number
  connections?: ExecutionConnection[]
  providers?: ExecutionProvider[]
  gate?: ExecutionGate
}
type ExecutionConnection = {
  connection_id?: string; provider_id?: string; label?: string; mode?: string
  state?: string; reason?: string | null; ready?: boolean; configured?: boolean
  implemented?: boolean; capabilities?: string[]; capability_limitations?: string[]
}
type ExecutionProvider = { provider_id?: string; label?: string; implemented?: boolean; supported_modes?: string[] }
type ExecutionGate = { paper_allowed?: boolean; live_allowed?: boolean; kill_switch_engaged?: boolean; manual_stop_engaged?: boolean }
export type Connections = { market_data: Section; trading_platform: Section; ai: Section; notifications: Section; tradingview: Section; execution_connections: Section }
type Check = { key: string; label: string; status: string; passed: boolean; detail: string }
export type Readiness = { status: string; live_mode: string; checks: Check[]; blockers: string[]; performance_qualification: string; measured_win_rate: number; evaluation_trades: number }

export type ConnectionsSnapshot = { connections: Connections | null; readiness: Readiness | null }

export default function ConnectionsPanel({ token, onLoad }: { token: string; onLoad?: (result: PanelLoad<ConnectionsSnapshot>) => void }) {
  const [connections, setConnections] = useState<Connections | null>(null)
  const [readiness, setReadiness] = useState<Readiness | null>(null)
  const [notice, setNotice] = useState('')
  const [pending, setPending] = useState('')
  const onLoadRef = useRef(onLoad)
  useEffect(() => { onLoadRef.current = onLoad })
  const refresh = async () => {
    try {
      const [c, r] = await Promise.all([fetch('/api/connections'), fetch('/api/readiness')])
      if (c.ok && r.ok) {
        const connectionPayload = await c.json() as Connections
        const readinessPayload = await r.json() as Readiness
        setConnections(connectionPayload)
        setReadiness(readinessPayload)
        onLoadRef.current?.({ data: { connections: connectionPayload, readiness: readinessPayload }, error: '' })
      } else {
        const message = c.ok ? 'readiness records are unavailable' : 'connection records are unavailable'
        onLoadRef.current?.({ data: null, error: message })
      }
    } catch {
      onLoadRef.current?.({ data: null, error: 'connection records are unavailable' })
    }
  }
  useEffect(() => { void refresh() }, [])
  const test = async (section: string) => {
    if (!token) { setNotice('Owner authentication is required to run connection tests.'); return }
    setPending(section); setNotice('')
    try {
      const response = await fetch('/api/connections/test', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Owner-Token': token }, body: JSON.stringify({ section }) })
      const result = await response.json()
      setNotice(result.message || result.error || 'No connection test available.')
      await refresh()
    } catch (cause) { setNotice(cause instanceof Error ? cause.message : 'Connection test failed.') }
    finally { setPending('') }
  }
  return <div className="connections-layout">
    <div className="connection-intro"><div><p className="eyebrow">ENVIRONMENT-BASED CONFIGURATION</p><h2>Connections &amp; Live Setup</h2><p>External services are not contacted automatically. Configure provider values in the local <code>.env</code> file, then restart. Secret values are never returned to the dashboard or stored in SQLite.</p></div><span className="locked-pill">LIVE LOCKED</span></div>
    {notice && <div className="alert">{notice}</div>}
    <div className="connection-cards">
      <ConnectionCard title="Market data" section="market_data" data={connections?.market_data} pending={pending} onTest={test} fields={[
        ['Provider', connections?.market_data.label ?? connections?.market_data.provider],
        ['Adapter', connections?.market_data.implemented ? 'Installed' : 'Not installed'],
        ['Connectivity', connections?.market_data.connectivity?.state ?? connections?.market_data.state ?? 'Unknown'],
        ['State detail', connections?.market_data.connectivity?.reason ?? connections?.market_data.reason ?? 'None'],
        ['Capabilities', capabilityList(connections?.market_data.capabilities)],
        ['Base URL', fieldState(connections?.market_data.base_url_configured)],
        ['Plan', fieldState(connections?.market_data.plan_configured)],
        ['Credentials', fieldState(connections?.market_data.configured)],
        ['Last successful', connections?.market_data.last_successful_connection ? new Date(connections.market_data.last_successful_connection).toLocaleString() : 'None'],
      ]} />
      <ConnectionCard title="Trading platform" section="trading_platform" data={connections?.trading_platform} pending={pending} onTest={test} fields={[
        ['Platform', connections?.trading_platform.name], ['Adapter', connections?.trading_platform.adapter], ['Environment', connections?.trading_platform.environment], ['Base URL', fieldState(connections?.trading_platform.base_url_configured)], ['Account ID', fieldState(connections?.trading_platform.account_id_configured)], ['Credentials', fieldState(connections?.trading_platform.configured && connections?.trading_platform.adapter !== 'mock')], ['Capabilities', capabilitySummary(connections?.trading_platform.capabilities)], ['Last successful', connections?.trading_platform.last_successful_connection ? new Date(connections.trading_platform.last_successful_connection).toLocaleString() : 'None'],
      ]} />
      <ConnectionCard title="AI provider" section="ai" data={connections?.ai} pending={pending} onTest={test} fields={[
        ['Provider', connections?.ai.provider], ['Model', connections?.ai.model], ['Endpoint', fieldState(connections?.ai.endpoint_configured)], ['Enabled', connections?.ai.enabled ? 'Yes' : 'No'], ['Authority', 'Advisory only'], ['Last successful', connections?.ai.last_successful_connection ? new Date(connections.ai.last_successful_connection).toLocaleString() : 'None'],
      ]} />
      <ConnectionCard title="Notifications" section="notifications" data={connections?.notifications} pending={pending} onTest={test} fields={[
        ['Provider', connections?.notifications.provider], ['Destination', fieldState(connections?.notifications.destination_configured)], ['Enabled', connections?.notifications.enabled ? 'Yes' : 'No'], ['Last successful', connections?.notifications.last_successful_connection ? new Date(connections.notifications.last_successful_connection).toLocaleString() : 'None'],
      ]} />
      <ConnectionCard title="TradingView" section="tradingview" data={connections?.tradingview} pending={pending} onTest={test} fields={[
        ['Endpoint', connections?.tradingview.endpoint], ['Role', connections?.tradingview.role], ['Enabled', connections?.tradingview.enabled ? 'Yes' : 'No'], ['Secret configured', fieldState(connections?.tradingview.secret_configured)], ['State', connections?.tradingview.state], ['State detail', connections?.tradingview.reason ?? 'None'], ['Signals', `${connections?.tradingview.signals_accepted ?? 0} accepted · ${connections?.tradingview.signals_rejected ?? 0} rejected · ${connections?.tradingview.signals_duplicate ?? 0} duplicate`], ['Strategies', (connections?.tradingview.allowed_strategies ?? []).length ? connections?.tradingview.allowed_strategies?.join(', ') : 'Any allowed strategy'], ['Last signal', connections?.tradingview.last_received_signal || 'None'],
      ]} />
      <ConnectionCard title="Execution provider" section="execution" data={connections?.execution_connections} pending={pending} onTest={test} fields={[
        ['State', connections?.execution_connections.state], ['State detail', connections?.execution_connections.reason ?? 'None'], ['Connections', `${connections?.execution_connections.ready_count ?? 0} of ${connections?.execution_connections.count ?? 0} ready`], ['Paper trading', connections?.execution_connections.gate?.paper_allowed ? 'Available (in-process)' : 'Blocked'], ['Live trading', connections?.execution_connections.gate?.live_allowed ? 'Available' : 'Locked in this build'], ['Providers', providerList(connections?.execution_connections.providers)], ['Capabilities', capabilityList(connections?.execution_connections.connections?.flatMap((item) => item.capabilities ?? []))], ['Limitations', capabilityList(connections?.execution_connections.connections?.flatMap((item) => item.capability_limitations ?? []))],
      ]} />
    </div>
    <div className="readiness-card"><div className="readiness-head"><div><p className="eyebrow">PRE-FLIGHT CHECKLIST</p><h3>Live readiness</h3></div><span className="locked-pill">{readiness?.live_mode ?? 'LOCKED'}</span></div>
      <p className="readiness-copy">Credentials alone cannot unlock live execution. No live adapter or activation control is installed in this version.</p>
      <div className="readiness-summary"><span>Performance: <b>{readiness?.performance_qualification ?? 'NOT_QUALIFIED'}</b></span><span>Evaluation: <b>{readiness?.evaluation_trades ?? 0} trades · {(readiness?.measured_win_rate ?? 0).toFixed(1)}%</b></span></div>
      <div className="check-grid">{readiness?.checks.map((check) => <div className="check-row" key={check.key}><i className={check.passed ? 'check-ok' : check.status === 'LOCKED' ? 'check-locked' : 'check-no'} />{check.label}<span>{check.status}</span></div>)}</div>
    </div>
    <p className="connection-footnote">Connection tests run in the backend only; this screen never contacts a provider or performs a network request. A configured provider reports real connectivity (CONNECTED, STALE, RATE_LIMITED, UNAVAILABLE) from recorded evidence, and no screen invents endpoints or prices.</p>
  </div>
}

function ConnectionCard({ title, section, data, pending, onTest, fields }: { title: string; section: string; data?: Section; pending: string; onTest: (section: string) => void; fields: [string, string | undefined][] }) {
  const result = data?.connection ?? data?.account_connection
  const configured = data?.ready ?? (data?.configured || data?.provider === 'LOCAL/MOCK' || data?.provider === 'MOCK')
  return <article className="connection-card"><div className="connection-card-head"><div><h3>{title}</h3><span className={`connection-state ${configured ? 'state-configured' : 'state-empty'}`}>{configured ? 'CONFIGURED' : 'NOT CONFIGURED'}</span></div><button className="test-connection" onClick={() => void onTest(section)} disabled={pending === section}>{pending === section ? 'Checking…' : 'Test connection'}</button></div><dl>{fields.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value || 'Not set'}</dd></div>)}<div><dt>Connection test</dt><dd className={result?.status === 'PASSED' ? 'positive' : ''}>{result?.status ?? 'NOT TESTED'}</dd></div>{result?.tested_at && <div><dt>Last test</dt><dd>{new Date(result.tested_at).toLocaleString()}</dd></div>}{result?.error && <div><dt>Test detail</dt><dd className="negative">{result.error}</dd></div>}</dl></article>
}

function fieldState(configured?: boolean) { return configured ? 'Configured (hidden)' : 'Not set' }
function capabilityList(value: unknown) {
  if (!Array.isArray(value) || !value.length) return 'None reported'
  const unique = Array.from(new Set(value.map((item) => String(item))))
  return unique.join(' · ')
}
function providerList(value: ExecutionProvider[] | undefined) {
  if (!value || !value.length) return 'None reported'
  return value.map((provider) => `${provider.label ?? provider.provider_id ?? 'unknown'}: ${provider.implemented ? 'installed' : 'not installed'}`).join(' · ')
}
function capabilitySummary(value: unknown) {
  if (!value || typeof value !== 'object') return 'Unavailable'
  const caps = value as Record<string, unknown>
  return `Orders ${caps.order_retrieval ? 'yes' : 'no'} · account ${caps.account_retrieval ? 'yes' : 'no'} · positions ${caps.position_retrieval ? 'yes' : 'no'} · market ${caps.market_data ? 'yes' : 'no'} · paper ${caps.paper_trading ? 'yes' : 'no'}`
}
