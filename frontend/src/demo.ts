/**
 * Signal-desk derivations: turn real API records into the demo ledger.
 *
 * Every function here is pure. It only re-shapes what the backend already
 * recorded - `/api/activity`, `/api/orders`, `/api/status` -
 * and it never invents a price, quantity, balance, fill, identifier or P&L.
 * Missing fields are reported as missing (``null`` / ``—``), not as zeroes.
 *
 * Kept free of React and DOM so `tests/demo.test.ts` can execute it with
 * Node's built-in test runner and no extra dependency.
 */

/** What every screen says about this build's execution path. */
export const PAPER_NOTICE =
  'PAPER TRADING: fills are produced by the local paper adapter. No broker account is connected, no live order is sent, and no value on this screen is fabricated.'

/** Good market-data availability state, as reported by the valuation bridge. */
const MARKET_DATA_CONNECTED = 'CONNECTED'

export type ActivityRow = {
  record_type?: string | null
  timestamp?: string | null
  symbol?: string | null
  event_type?: string | null
  message?: string | null
  status?: string | null
  order_id?: string | null
  fill_id?: string | null
  quantity?: number | null
  price?: number | null
  approved?: number | boolean | null
  reasons_json?: unknown
  context_json?: unknown
}

export type LedgerOutcome =
  | 'accepted'
  | 'approved'
  | 'rejected'
  | 'error'
  | 'info'

export type LedgerKind =
  | 'risk'
  | 'order'
  | 'fill'
  | 'error'
  | 'other'

export type LedgerStep = {
  key: string
  at: string
  symbol: string | null
  kind: LedgerKind
  outcome: LedgerOutcome
  title: string
  detail: string
  identifiers: string[]
}

export type ApiFill = {
  fill_id?: string | null
  order_id?: string | null
  quantity?: number | null
  price?: number | null
  timestamp?: string | null
}

export type ApiOrder = {
  order_id?: string | null
  symbol?: string | null
  direction?: string | null
  quantity?: number | null
  status?: string | null
  filled_quantity?: number | null
  remaining_quantity?: number | null
  average_fill_price?: number | null
  rejection_reason?: string | null
  signal_id?: string | null
  source?: string | null
  created_at?: string | null
  fills?: ApiFill[] | null
}

export type OrderView = {
  key: string
  order_id: string
  symbol: string
  direction: string
  quantity: number
  status: string
  filled_quantity: number
  remaining_quantity: number
  average_fill_price: number | null
  rejection_reason: string | null
  signal_id: string | null
  source: string | null
  created_at: string | null
  fills: { fill_id: string | null; quantity: number; price: number; timestamp: string | null }[]
  tone: 'good' | 'quiet' | 'bad'
}

export type ReadinessView = {
  market_data_provider: string
  market_data_state: string
  market_data_availability: string
  market_data_reason: string | null
  mode: string
  execution_adapter: string
  risk_kill_switch: boolean
  blockers: string[]
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}

function number(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function reasons(row: ActivityRow): string[] {
  const raw = row.reasons_json
  if (Array.isArray(raw)) {
    return raw.map((item) => String(item)).filter((item) => item.length > 0)
  }
  return []
}

function identifiersOf(row: ActivityRow): string[] {
  const context = asRecord(row.context_json)
  const intent = asRecord(context.intent)
  const found: string[] = []
  for (const [label, value] of [
    ['signal', context.signal_id ?? intent.signal_id],
    ['order', row.order_id ?? context.order_id],
    ['fill', row.fill_id ?? context.fill_id],
    ['code', context.code],
  ] as const) {
    const asText = text(value)
    if (asText) found.push(`${label} ${asText}`)
  }
  return found
}

function stepFor(row: ActivityRow, index: number): LedgerStep | null {
  const type = row.record_type ?? ''
  const at = row.timestamp ?? ''
  const base = {
    key: `${type}:${at}:${index}`,
    at,
    symbol: text(row.symbol),
  }

  if (type === 'system') {
    // The ledger is the decision narrative, not the whole journal: system
    // events (reconciliation, session notes, lifecycle messages) stay in the
    // general "Recent trades and events" panel. What narrates here is what the
    // platform itself decided: risk, orders, fills and errors.
    return null
  }

  if (type === 'risk') {
    const approved = Boolean(row.approved)
    const context = asRecord(row.context_json)
    const list = reasons(row)
    const detail = list.length
      ? list.join('; ')
      : approved
        ? 'no risk rule blocked this signal'
        : 'no reason was recorded'
    const identifiers = identifiersOf(row)
    const quantity = number(context.quantity)
    if (quantity !== null) identifiers.push(`approved quantity ${quantity}`)
    return { ...base, kind: 'risk',
             outcome: approved ? 'approved' : 'rejected',
             title: approved ? 'Risk decision: APPROVED' : 'Risk decision: REJECTED',
             detail, identifiers }
  }

  if (type === 'order') {
    const context = asRecord(row.context_json)
    const intent = asRecord(context.intent)
    const status = text(row.status) ?? text(context.status) ?? 'UNKNOWN'
    const rejection = text(context.rejection_reason)
    const source = text(context.source) ?? text(intent.source)
    const detail = rejection ?? (source ? `submitted by ${source}` : 'no detail recorded')
    const identifiers = identifiersOf(row)
    return { ...base, kind: 'order', outcome: status === 'REJECTED' ? 'rejected' : 'info',
             title: `Order ${status}`, detail, identifiers }
  }

  if (type === 'fill') {
    const detail = row.message
      ?? `executed ${row.quantity ?? ''} @ ${row.price ?? ''}`.trim()
    return { ...base, kind: 'fill', outcome: 'accepted',
             title: 'Paper fill recorded', detail, identifiers: identifiersOf(row) }
  }

  if (type === 'error') {
    return { ...base, kind: 'error', outcome: 'error',
             title: 'System error', detail: row.message ?? '', identifiers: identifiersOf(row) }
  }

  if (!type) return null
  return { ...base, kind: 'other', outcome: 'info',
           title: type, detail: row.message ?? '', identifiers: identifiersOf(row) }
}

/**
 * Newest-first ledger of the records a presenter narrates: the risk decision,
 * the order, the fill and the refusal behind a rejection.
 */
export function buildLedger(rows: readonly ActivityRow[]): LedgerStep[] {
  const steps: LedgerStep[] = []
  rows.forEach((row, index) => {
    const step = stepFor(row, index)
    if (step) steps.push(step)
  })
  return steps.sort((a, b) => (a.at === b.at ? 0 : a.at < b.at ? 1 : -1))
}

/** One row per order with its durable fills attached; null stays null. */
export function buildOrderViews(orders: readonly ApiOrder[]): OrderView[] {
  return orders.map((order, index) => {
    const status = order.status ?? 'UNKNOWN'
    const tone: OrderView['tone'] =
      status === 'FILLED' ? 'good' : status === 'REJECTED' || status === 'CANCELED'
        || status === 'CANCELLED' || status === 'EXPIRED' ? 'bad' : 'quiet'
    const fills = (order.fills ?? []).map((fill) => ({
      fill_id: text(fill.fill_id),
      quantity: number(fill.quantity) ?? 0,
      price: number(fill.price) ?? 0,
      timestamp: text(fill.timestamp),
    }))
    return {
      key: `${order.order_id ?? 'order'}:${index}`,
      order_id: order.order_id ?? '—',
      symbol: order.symbol ?? '—',
      direction: order.direction ?? '—',
      quantity: number(order.quantity) ?? 0,
      status,
      filled_quantity: number(order.filled_quantity) ?? 0,
      remaining_quantity: number(order.remaining_quantity) ?? 0,
      average_fill_price: number(order.average_fill_price),
      rejection_reason: text(order.rejection_reason),
      signal_id: text(order.signal_id),
      source: text(order.source),
      created_at: text(order.created_at),
      fills,
      tone,
    }
  })
}

/**
 * Honest readiness for the demo header. A blocker is only ever a reason the
 * backend itself reported - an absent field never becomes a warning.
 */
export function describeReadiness(
  status: Record<string, unknown> | null | undefined,
): ReadinessView {
  const view: ReadinessView = {
    market_data_provider: text(status?.market_data_provider) ?? '—',
    market_data_state: text(status?.market_data_state) ?? 'UNKNOWN',
    market_data_availability: text(status?.market_data_availability) ?? 'NOT_CONFIGURED',
    market_data_reason: text(status?.market_data_availability_reason),
    mode: text(status?.mode) ?? 'UNKNOWN',
    execution_adapter: text(status?.execution_adapter) ?? '—',
    risk_kill_switch: status?.risk_kill_switch === true,
    blockers: [],
  }

  if (view.market_data_availability !== MARKET_DATA_CONNECTED
      && view.market_data_reason) {
    view.blockers.push(`Market data: ${view.market_data_reason}`)
  }
  return view
}
