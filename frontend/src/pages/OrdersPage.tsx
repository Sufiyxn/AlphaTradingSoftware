/**
 * #/orders — the existing signal desk (orders, fills, ledger from
 * /api/orders and /api/activity) behind an explicit loading/error gate plus
 * a small execution summary derived from the same records.
 */
import { useState } from 'react'
import { ErrorState, LoadingState } from '../components'
import type { PageProps } from '../components'
import SignalDesk from '../SignalDesk'
import type { SignalDeskSnapshot } from '../SignalDesk'
import { classifyApiPhase } from '../routes'
import type { PanelLoad } from '../types'

const TERMINAL_STATUSES = ['FILLED', 'REJECTED', 'CANCELED', 'CANCELLED', 'EXPIRED']

export default function OrdersPage({ status, refresh }: PageProps) {
  const [desk, setDesk] = useState<PanelLoad<SignalDeskSnapshot>>({ data: null, error: '' })
  const phase = classifyApiPhase(desk.data !== null, desk.error !== '')
  const orders = desk.data?.orders ?? []
  const ledger = desk.data?.ledger ?? []
  const openOrders = orders.filter((order) => !TERMINAL_STATUSES.includes(order.status)).length
  const filledOrders = orders.filter((order) => order.status === 'FILLED').length
  const rejectedOrders = orders.filter((order) => order.status === 'REJECTED').length
  const fillsRecorded = ledger.filter((step) => step.kind === 'fill').length

  // The desk owns the fetch that reports back through `onLoad`, so it must be
  // mounted *before* the phase can leave `loading` — gating it on `ready`
  // would be a deadlock (the page waits for data only the hidden panel can
  // request). Same pattern as the Watchlists page: mounted hidden, revealed
  // by its own first payload.
  return <>
    {phase === 'loading' && <LoadingState label="Loading orders, fills and the signal ledger…" />}
    {phase === 'error' && <ErrorState what="Order and activity records" message={desk.error} />}
    {phase === 'ready' && <section className="panel extra-section">
      <div className="panel-head">
        <div>
          <p className="eyebrow">EXECUTION SUMMARY · PAPER ADAPTER</p>
          <h2>Session order flow</h2>
        </div>
        <span className="count-badge">{orders.length} orders</span>
      </div>
      <div className="count-strip">
        <span>Total orders <b>{orders.length}</b></span>
        <span>Open <b>{openOrders}</b></span>
        <span>Filled <b>{filledOrders}</b></span>
        <span>Rejected <b>{rejectedOrders}</b></span>
        <span>Fills recorded <b>{fillsRecorded}</b></span>
      </div>
      {orders.length === 0 && <p className="panel-foot">No orders have been submitted in this journal yet. Every order and fill the paper adapter records appears below.</p>}
    </section>}
    <div hidden={phase !== 'ready'}>
      <SignalDesk
        key={refresh}
        status={status ?? undefined}
        onLoad={(result) => setDesk((previous) => ({ data: result.data ?? previous.data, error: result.error }))}
      />
    </div>
  </>
}
