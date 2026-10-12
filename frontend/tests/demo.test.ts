/**
 * Signal-desk tests, run with Node's built-in runner (no added dependency):
 *
 *     node --test tests/
 *
 * The first block feeds recorded API shapes through the pure derivations in
 * `src/demo.ts`. The second block, `live-api`, is skipped unless
 * `ALPHA_API_BASE` points at a running backend, so `node --test` stays green
 * without a server while still offering a real end-to-end check.
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  PAPER_NOTICE,
  buildLedger,
  buildOrderViews,
  describeReadiness,
} from '../src/demo.ts'
import type { ActivityRow, ApiOrder } from '../src/demo.ts'

const RISK_APPROVED: ActivityRow = {
  record_type: 'risk',
  timestamp: '2024-01-02T15:30:01+00:00',
  symbol: 'AAPL',
  approved: 1,
  reasons_json: [],
  message: 'risk decision: APPROVED',
  context_json: { code: 'APPROVED', quantity: 5, status: 'APPROVED' },
}

const RISK_REJECTED: ActivityRow = {
  record_type: 'risk',
  timestamp: '2024-01-02T15:31:00+00:00',
  symbol: 'MSFT',
  approved: 0,
  reasons_json: ['total trade risk exceeds the per-trade risk budget'],
  message: 'risk decision: REJECTED',
  context_json: { code: 'TRADE_RISK_EXCEEDED', quantity: 0, status: 'REJECTED' },
}

const ORDER: ActivityRow = {
  record_type: 'order',
  timestamp: '2024-01-02T15:30:02+00:00',
  symbol: 'AAPL',
  order_id: 'CLIENT-1',
  status: 'FILLED',
  context_json: {
    order_id: 'CLIENT-1',
    status: 'FILLED',
    rejection_reason: null,
    intent: { source: 'orchestrator', signal_id: 'id:cycle-7', quantity: 5 },
  },
}

const FILL: ActivityRow = {
  record_type: 'fill',
  timestamp: '2024-01-02T15:30:03+00:00',
  symbol: 'AAPL',
  fill_id: 'fill-9',
  order_id: 'CLIENT-1',
  quantity: 5,
  price: 50.01,
  message: 'executed 5 @ 50.01',
}

test('ledger narrates one approved decision from risk to fill', () => {
  const ledger = buildLedger([FILL, ORDER, RISK_APPROVED])
  assert.deepEqual(
    ledger.map((step) => step.title),
    ['Paper fill recorded', 'Order FILLED', 'Risk decision: APPROVED'],
  )
  const order = ledger[1]
  assert.equal(order.kind, 'order')
  assert.equal(order.detail, 'submitted by orchestrator', 'order context nests under intent')
  assert.deepEqual(order.identifiers, ['signal id:cycle-7', 'order CLIENT-1'])
  assert.equal(ledger[0].identifiers.includes('fill fill-9'), true)
  assert.equal(ledger[2].identifiers.includes('approved quantity 5'), true)
})

test('ledger keeps the real rejection reason instead of a generic label', () => {
  const ledger = buildLedger([RISK_REJECTED])
  assert.equal(ledger[0].outcome, 'rejected')
  assert.equal(ledger[0].title, 'Risk decision: REJECTED')
  assert.equal(ledger[0].detail, 'total trade risk exceeds the per-trade risk budget')
  assert.deepEqual(ledger[0].identifiers, ['code TRADE_RISK_EXCEEDED', 'approved quantity 0'])
})

test('ledger keeps decisions, fills and errors, and drops journal noise', () => {
  const ledger = buildLedger([
    { record_type: 'system', timestamp: '2024-01-02T15:30:00+00:00', event_type: 'RECONCILIATION_CLEAN' },
    { record_type: '', timestamp: '2024-01-02T15:40:00+00:00', message: 'ignored' },
    { record_type: 'error', timestamp: '2024-01-02T15:35:00+00:00', message: 'boom' },
    { record_type: 'system', timestamp: '2024-01-02T15:36:00+00:00', event_type: 'ENGINE_CYCLE', message: 'ok' },
  ])
  assert.deepEqual(
    ledger.map((step) => step.kind),
    ['error'],
    'newest first; system events and empty records stay in the journal panel, not the ledger',
  )
  assert.equal(ledger[0].detail, 'boom')
})

test('order views carry identifiers, fill history and an honest status tone', () => {
  const orders: ApiOrder[] = [{
    order_id: 'CLIENT-1',
    symbol: 'AAPL',
    direction: 'LONG',
    quantity: 10,
    status: 'PARTIALLY_FILLED',
    filled_quantity: 4,
    remaining_quantity: 6,
    average_fill_price: null,
    rejection_reason: null,
    signal_id: 'id:cycle-7',
    source: 'orchestrator',
    created_at: '2024-01-02T15:30:02+00:00',
    fills: [
      { fill_id: 'fill-9', quantity: 4, price: 50.01, timestamp: '2024-01-02T15:30:03+00:00' },
    ],
  }, {
    order_id: 'CLIENT-2',
    symbol: 'MSFT',
    direction: 'LONG',
    quantity: 5,
    status: 'REJECTED',
    filled_quantity: 0,
    remaining_quantity: 0,
    average_fill_price: null,
    rejection_reason: 'rejected by risk',
    source: 'orchestrator',
    fills: [],
  }]
  const views = buildOrderViews(orders)
  assert.equal(views[0].tone, 'quiet')
  assert.equal(views[0].average_fill_price, null, 'no fabricated average price')
  assert.equal(views[0].fills.length, 1)
  assert.equal(views[0].fills[0].fill_id, 'fill-9')
  assert.equal(views[1].tone, 'bad')
  assert.equal(views[1].rejection_reason, 'rejected by risk')
})

test('readiness reports blockers only from backend reasons', () => {
  const healthy = describeReadiness(
    { mode: 'PAPER', market_data_provider: 'alpaca', market_data_state: 'READY',
      market_data_availability: 'CONNECTED', execution_adapter: 'paper', risk_kill_switch: false },
  )
  assert.equal(healthy.market_data_availability, 'CONNECTED')
  assert.equal(healthy.mode, 'PAPER')
  assert.equal(healthy.blockers.length, 0)

  const stale = describeReadiness(
    { mode: 'PAPER', market_data_availability: 'STALE',
      market_data_availability_reason: 'every available quote is older than the configured staleness window' },
  )
  assert.deepEqual(stale.blockers, [
    'Market data: every available quote is older than the configured staleness window',
  ])
})

test('readiness without data reports no invented blocker', () => {
  const view = describeReadiness(null)
  assert.equal(view.market_data_availability, 'NOT_CONFIGURED')
  assert.equal(view.mode, 'UNKNOWN')
  assert.deepEqual(view.blockers, [])
})

test('the paper notice never claims a broker or live order', () => {
  assert.match(PAPER_NOTICE, /PAPER TRADING/)
  assert.match(PAPER_NOTICE, /No broker account is connected/)
})

// -- optional live check: ALPHA_API_BASE=http://127.0.0.1:8000 ---------------

const base = process.env.ALPHA_API_BASE

test('live API: derived views match a running backend', { skip: !base }, async () => {
  const read = async (path: string) => {
    const response = await fetch(new URL(path, base))
    assert.equal(response.ok, true, `${path} should return 200`)
    return response.json()
  }
  const [orders, activity, connections, status] = await Promise.all([
    read('/api/orders'),
    read('/api/activity?limit=200'),
    read('/api/connections'),
    read('/api/status'),
  ])

  const orderViews = buildOrderViews((orders as { orders: ApiOrder[] }).orders ?? [])
  for (const view of orderViews) {
    assert.notEqual(view.order_id, '')
    assert.equal(typeof view.status, 'string')
    assert.equal(view.fills.length <= view.quantity || view.quantity === 0, true)
  }

  const ledger = buildLedger((activity as { events: ActivityRow[] }).events ?? [])
  for (const step of ledger) {
    assert.notEqual(step.title, '')
    assert.equal(step.detail.includes('undefined'), false)
  }

  const readiness = describeReadiness(status as Record<string, unknown>)
  assert.notEqual(readiness.mode, 'UNKNOWN')
  assert.equal(typeof readiness.risk_kill_switch, 'boolean')
})
