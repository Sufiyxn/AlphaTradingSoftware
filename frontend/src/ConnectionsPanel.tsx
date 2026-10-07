import { useEffect, useState } from 'react'

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
}
type Connections = { market_data: Section; trading_platform: Section; ai: Section; notifications: Section; tradingview: Section }
type Check = { key: string; label: string; status: string; passed: boolean; detail: string }
type Readiness = { status: string; live_mode: string; checks: Check[]; blockers: string[]; performance_qualification: string; measured_win_rate: number; evaluation_trades: number }

export default function ConnectionsPanel({ token }: { token: string }) {
  const [connections, setConnections] = useState<Connections | null>(null)
  const [readiness, setReadiness] = useState<Readiness | null>(null)
  const [notice, setNotice] = useState('')
  const [pending, setPending] = useState('')
  const refresh = async () => {
    const [c, r] = await Promise.all([fetch('/api/connections'), fetch('/api/readiness')])
    if (c.ok && r.ok) { setConnections(await c.json()); setReadiness(await r.json()) }
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
        ['Provider', connections?.market_data.provider], ['Mode', connections?.market_data.mode], ['Base URL', fieldState(connections?.market_data.base_url_configured)], ['Plan', fieldState(connections?.market_data.plan_configured)], ['Data account', fieldState(connections?.market_data.account_id_configured)], ['Credentials', fieldState(connections?.market_data.configured && connections?.market_data.provider !== 'MOCK')], ['Last successful', connections?.market_data.last_successful_connection ? new Date(connections.market_data.last_successful_connection).toLocaleString() : 'None'],
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
    </div>
    <div className="readiness-card"><div className="readiness-head"><div><p className="eyebrow">PRE-FLIGHT CHECKLIST</p><h3>Live readiness</h3></div><span className="locked-pill">{readiness?.live_mode ?? 'LOCKED'}</span></div>
      <p className="readiness-copy">Credentials alone cannot unlock live execution. No live adapter or activation control is installed in this version.</p>
      <div className="readiness-summary"><span>Performance: <b>{readiness?.performance_qualification ?? 'NOT_QUALIFIED'}</b></span><span>Evaluation: <b>{readiness?.evaluation_trades ?? 0} trades · {(readiness?.measured_win_rate ?? 0).toFixed(1)}%</b></span></div>
      <div className="check-grid">{readiness?.checks.map((check) => <div className="check-row" key={check.key}><i className={check.passed ? 'check-ok' : check.status === 'LOCKED' ? 'check-locked' : 'check-no'} />{check.label}<span>{check.status}</span></div>)}</div>
    </div>
    <p className="connection-footnote">Local Mock connections can be checked without credentials. External connection tests report unsupported until an official provider adapter is installed; this screen will not invent endpoints or make network calls.</p>
  </div>
}

function ConnectionCard({ title, section, data, pending, onTest, fields }: { title: string; section: string; data?: Section; pending: string; onTest: (section: string) => void; fields: [string, string | undefined][] }) {
  const result = data?.connection ?? data?.account_connection
  const configured = data?.ready ?? (data?.configured || data?.provider === 'LOCAL/MOCK' || data?.provider === 'MOCK')
  return <article className="connection-card"><div className="connection-card-head"><div><h3>{title}</h3><span className={`connection-state ${configured ? 'state-configured' : 'state-empty'}`}>{configured ? 'CONFIGURED' : 'NOT CONFIGURED'}</span></div><button className="test-connection" onClick={() => void onTest(section)} disabled={pending === section}>{pending === section ? 'Checking…' : 'Test connection'}</button></div><dl>{fields.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value || 'Not set'}</dd></div>)}<div><dt>Connection test</dt><dd className={result?.status === 'PASSED' ? 'positive' : ''}>{result?.status ?? 'NOT TESTED'}</dd></div>{result?.tested_at && <div><dt>Last test</dt><dd>{new Date(result.tested_at).toLocaleString()}</dd></div>}{result?.error && <div><dt>Test detail</dt><dd className="negative">{result.error}</dd></div>}</dl></article>
}

function fieldState(configured?: boolean) { return configured ? 'Configured (hidden)' : 'Not set' }
function capabilitySummary(value: unknown) {
  if (!value || typeof value !== 'object') return 'Unavailable'
  const caps = value as Record<string, unknown>
  return `Orders ${caps.order_retrieval ? 'yes' : 'no'} · account ${caps.account_retrieval ? 'yes' : 'no'} · positions ${caps.position_retrieval ? 'yes' : 'no'} · market ${caps.market_data ? 'yes' : 'no'} · paper ${caps.paper_trading ? 'yes' : 'no'}`
}
