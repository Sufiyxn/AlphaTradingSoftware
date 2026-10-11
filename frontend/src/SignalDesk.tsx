import { useCallback, useEffect, useRef, useState } from 'react'
import {
  PAPER_NOTICE,
  buildLedger,
  buildOrderViews,
  describeReadiness,
} from './demo'
import type { ActivityRow, ApiOrder, LedgerStep, OrderView, ReadinessView } from './demo'
import type { PanelLoad } from './types'

export type SignalDeskSnapshot = { orders: OrderView[]; ledger: LedgerStep[] }

type StatusSlice = {
  mode?: string | null
  market_data_provider?: string | null
  market_data_state?: string | null
  market_data_availability?: string | null
  market_data_availability_reason?: string | null
  execution_adapter?: string | null
  risk_kill_switch?: boolean | null
}

type OrdersPayload = { orders?: ApiOrder[]; count?: number }
type ActivityPayload = { events?: ActivityRow[] }
type ConnectionsPayload = Record<string, unknown>
type PositionSlice = {
  symbol?: string
  quantity?: number
  entry_price?: number
  unrealized_pnl?: number | null
}
type PositionsPayload = { positions?: PositionSlice[] }

const usd = (n?: number | null) =>
  n == null ? '—' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n)
const time = (s?: string | null) => (s ? new Date(s).toLocaleString() : '—')

export default function SignalDesk({ status, onLoad }: { status?: StatusSlice; onLoad?: (result: PanelLoad<SignalDeskSnapshot>) => void }) {
  const [orders, setOrders] = useState<OrderView[]>([])
  const [ledger, setLedger] = useState<LedgerStep[]>([])
  const [connections, setConnections] = useState<ConnectionsPayload | null>(null)
  const [positions, setPositions] = useState<PositionSlice[]>([])
  const [error, setError] = useState('')
  const onLoadRef = useRef(onLoad)
  useEffect(() => { onLoadRef.current = onLoad })

  const refresh = useCallback(async () => {
    try {
      const responses = await Promise.all([
        fetch('/api/orders'),
        fetch('/api/activity?limit=200'),
        fetch('/api/connections'),
        fetch('/api/positions'),
      ])
      if (responses.some((response) => !response.ok)) {
        throw new Error('order and activity records are unavailable')
      }
      const [orderPayload, activityPayload, connectionPayload, positionPayload] =
        await Promise.all(responses.map((response) => response.json())) as [
          OrdersPayload, ActivityPayload, ConnectionsPayload, PositionsPayload,
        ]
      const orderViews = buildOrderViews(orderPayload.orders ?? [])
      const ledgerSteps = buildLedger(activityPayload.events ?? [])
      setOrders(orderViews)
      setLedger(ledgerSteps)
      setConnections(connectionPayload)
      setPositions(positionPayload.positions ?? [])
      setError('')
      onLoadRef.current?.({ data: { orders: orderViews, ledger: ledgerSteps }, error: '' })
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : 'API unavailable'
      setError(message)
      onLoadRef.current?.({ data: null, error: message })
    }
  }, [])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), 10000)
    return () => window.clearInterval(timer)
  }, [refresh])

  const readiness: ReadinessView = describeReadiness(connections, status)
  const positionBySymbol = new Map(
    positions.filter((item) => typeof item.symbol === 'string')
      .map((item) => [item.symbol as string, item]),
  )

  return (
    <section className="panel recent-panel" id="orders">
      <div className="panel-head">
        <div>
          <p className="eyebrow">TRADINGVIEW SIGNAL DESK · PAPER EXECUTION</p>
          <h2>Alerts, risk decisions, orders and fills</h2>
        </div>
        <div className="log-tools">
          <span className={`status-tag ${readiness.webhook_ready ? 'good' : 'quiet'}`}>
            WEBHOOK {readiness.webhook_state}
          </span>
          <a className="text-link" href="/api/logs?kind=fills&amp;format=json" target="_blank" rel="noreferrer">
            Fills JSON ↗
          </a>
          <a className="text-link" href="/api/logs?kind=orders&amp;format=json" target="_blank" rel="noreferrer">
            Orders JSON ↗
          </a>
        </div>
      </div>

      <div className="readiness-summary">
        <span>
          Webhook: <b>{readiness.webhook_state}</b> · {readiness.signals_accepted} accepted ·{' '}
          {readiness.signals_rejected} rejected · {readiness.signals_duplicate} duplicate
        </span>
        <span>
          Market data: <b>{readiness.market_data_availability}</b> · {readiness.market_data_provider}
        </span>
        <span>
          Mode: <b>{readiness.mode}</b> · execution {readiness.execution_adapter}
        </span>
      </div>
      <p className="readiness-copy">
        Endpoint {readiness.webhook_endpoint} authenticates each alert with an HMAC shared secret.
        TradingView is a signal source only: it is never a broker, a market-data provider, or a price source.
        {readiness.last_rejection_reason && <> Last rejection: <b>{readiness.last_rejection_reason}</b>.</>}
      </p>

      {error && <div className="alert danger">Signal desk unavailable: {error}.</div>}
      {readiness.blockers.map((blocker) => (
        <div className="alert" key={blocker}>{blocker}</div>
      ))}

      <div className="panel-head">
        <div>
          <p className="eyebrow">IDENTIFIERS AND STATUS</p>
          <h3>Order and fill history</h3>
        </div>
        <span className="count-badge">{orders.length}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Order</th>
              <th>Signal</th>
              <th>Symbol</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Filled</th>
              <th>Avg price</th>
              <th>Fills</th>
              <th>Position</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((order) => {
              const position = positionBySymbol.get(order.symbol)
              return (
              <tr key={order.key}>
                <td className="symbol">{order.order_id}</td>
                <td>{order.signal_id ?? '—'}</td>
                <td className="symbol">{order.symbol}</td>
                <td><span className={`direction ${order.direction.toLowerCase()}`}>{order.direction}</span></td>
                <td>{order.quantity}</td>
                <td>{order.filled_quantity} <span className="muted">/ {order.remaining_quantity} left</span></td>
                <td>{order.average_fill_price == null ? '—' : usd(order.average_fill_price)}</td>
                <td className="event-message">
                  {order.fills.length
                    ? order.fills.map((fill) => (
                        <div key={fill.fill_id ?? fill.timestamp ?? String(fill.quantity)}>
                          {fill.fill_id ?? 'fill id not reported'} · {fill.quantity} @ {usd(fill.price)}
                        </div>
                      ))
                    : '—'}
                </td>
                <td>
                  {position
                    ? <>{position.quantity} @ {usd(position.entry_price)}</>
                    : '—'}
                </td>
                <td>
                  <span className={`status-tag ${order.tone === 'good' ? 'good' : order.tone === 'bad' ? 'quiet negative' : 'quiet'}`}>{order.status}</span>
                  {order.rejection_reason && <div className="event-message">{order.rejection_reason}</div>}
                </td>
              </tr>
              )
            })}
            {!orders.length && <Empty span={10} text="No orders have been submitted in this journal." />}
          </tbody>
        </table>
      </div>

      <div className="panel-head">
        <div>
          <p className="eyebrow">WHAT THE PRESENTER NARRATES</p>
          <h3>Signal ledger</h3>
        </div>
        <span className="count-badge">{ledger.length}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Event</th>
              <th>Symbol</th>
              <th>Detail</th>
              <th>Identifiers</th>
            </tr>
          </thead>
          <tbody>
            {ledger.map((step) => (
              <tr key={step.key}>
                <td>{time(step.at)}</td>
                <td>
                  <span className={`status-tag ${toneOf(step)}`}>{step.title}</span>
                </td>
                <td className="symbol">{step.symbol ?? '—'}</td>
                <td className="event-message">{step.detail || '—'}</td>
                <td className="event-message">
                  {step.identifiers.length ? step.identifiers.join(' · ') : '—'}
                </td>
              </tr>
            ))}
            {!ledger.length && <Empty span={5} text="No signal, risk, order or fill records exist yet." />}
          </tbody>
        </table>
      </div>

      <p className="panel-foot">{PAPER_NOTICE}</p>
      <p className="panel-foot">
        Every row above is read from the local SQLite journal through <code>/api/orders</code>,{' '}
        <code>/api/activity</code> and <code>/api/connections</code>. Nothing on this screen is generated
        by the browser, and unpriced values stay unpriced.
      </p>
    </section>
  )
}

function toneOf(step: LedgerStep): string {
  if (step.outcome === 'approved' || step.outcome === 'accepted') return 'good'
  if (step.outcome === 'rejected' || step.outcome === 'error') return 'quiet negative'
  return 'quiet'
}

function Empty({ span, text }: { span: number; text: string }) {
  return <tr><td colSpan={span} className="empty-state">{text}</td></tr>
}
