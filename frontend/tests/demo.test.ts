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

const ALERT: ActivityRow = {
  record_type: 'system',
  timestamp: '2024-01-02T15:30:01+00:00',
  symbol: 'AAPL',
  event_type: 'TV_SIGNAL_RECEIVED',
  message: 'TradingView entry signal accepted for AAPL (5 @ 50)',
  context_json: {
    signal_id: 'id:tv-1',
    order_id: 'CLIENT-1',
    symbol: 'AAPL',
    action: 'ENTRY',
    quantity: 5,
    entry_price: 50,
    status: 'accepted',
    code: 'ACCEPTED',
  },
}

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
    intent: { source: 'tradingview', signal_id: 'id:tv-1', quantity: 5 },
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

const DUPLICATE: ActivityRow = {
  record_type: 'system',
  timestamp: '2024-01-02T15:30:04+00:00',
  symbol: 'AAPL',
  event_type: 'TV_SIGNAL_DUPLICATE',
  message: 'duplicate TradingView signal ignored',
  context_json: { signal_id: 'id:tv-1', symbol: 'AAPL' },
}

const REJECTED: ActivityRow = {
  record_type: 'system',
  timestamp: '2024-01-02T15:32:00+00:00',
  symbol: 'MSFT',
  event_type: 'TV_SIGNAL_REJECTED',
  message: 'TradingView signal rejected',
  context_json: {
    code: 'RISK_REJECTED',
    reason: 'TRADE_RISK_EXCEEDED',
    message: 'the signal was rejected by the risk engine',
    signal_id: 'id:tv-2',
  },
}

test('ledger narrates one accepted alert from alert to fill', () => {
  const ledger = buildLedger([FILL, ORDER, RISK_APPROVED, ALERT])
  assert.deepEqual(
    ledger.map((step) => step.title),
    ['Paper fill recorded', 'Order FILLED', 'Risk decision: APPROVED', 'TradingView alert accepted'],
  )
  const alert = ledger[3]
  assert.equal(alert.kind, 'alert')
  assert.equal(alert.outcome, 'accepted')
  assert.deepEqual(alert.identifiers, ['signal id:tv-1', 'order CLIENT-1', 'code ACCEPTED'])
  assert.equal(ledger[0].identifiers.includes('fill fill-9'), true)
  assert.equal(ledger[1].title, 'Order FILLED')
  assert.equal(ledger[1].detail, 'submitted by tradingview', 'order context nests under intent')
  assert.deepEqual(ledger[1].identifiers, ['signal id:tv-1', 'order CLIENT-1'])
  assert.equal(ledger[2].identifiers.includes('approved quantity 5'), true)
})

test('ledger keeps the real rejection reason instead of a generic label', () => {
  const ledger = buildLedger([RISK_REJECTED])
  assert.equal(ledger[0].outcome, 'rejected')
  assert.equal(ledger[0].title, 'Risk decision: REJECTED')
  assert.equal(ledger[0].detail, 'total trade risk exceeds the per-trade risk budget')
  assert.deepEqual(ledger[0].identifiers, ['code TRADE_RISK_EXCEEDED', 'approved quantity 0'])
})

test('ledger separates a refused duplicate from an accepted alert', () => {
  const ledger = buildLedger([DUPLICATE, ALERT])
  assert.equal(ledger[0].kind, 'duplicate')
  assert.equal(ledger[0].outcome, 'duplicate')
  assert.equal(ledger[0].title, 'Duplicate alert refused')
  assert.equal(ledger[1].outcome, 'accepted')
  assert.notEqual(ledger[0].outcome, ledger[1].outcome)
})

test('ledger surfaces a rejected signal with its backend code and message', () => {
  const ledger = buildLedger([REJECTED])
  assert.equal(ledger[0].kind, 'rejected')
  assert.equal(ledger[0].outcome, 'rejected')
  assert.equal(ledger[0].title, 'Alert rejected · TRADE_RISK_EXCEEDED')
  assert.equal(ledger[0].detail, 'the signal was rejected by the risk engine')
  assert.deepEqual(ledger[0].identifiers, ['signal id:tv-2', 'code RISK_REJECTED'])
})

test('ledger keeps signal events and errors, and drops unrelated journal noise', () => {
  const ledger = buildLedger([
    { record_type: 'system', timestamp: '2024-01-02T15:30:00+00:00', event_type: 'RECONCILIATION_CLEAN' },
    { record_type: '', timestamp: '2024-01-02T15:40:00+00:00', message: 'ignored' },
    { record_type: 'error', timestamp: '2024-01-02T15:35:00+00:00', message: 'boom' },
    { record_type: 'system', timestamp: '2024-01-02T15:36:00+00:00', event_type: 'TV_SIGNAL_RECEIVED', message: 'ok' },
  ])
  assert.deepEqual(
    ledger.map((step) => step.kind),
    ['alert', 'error'],
    'newest first; only TradingView signal events and errors narrate the demo',
  )
  assert.equal(ledger[1].detail, 'boom')
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
    signal_id: 'id:tv-1',
    source: 'tradingview',
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
    source: 'tradingview',
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
    { tradingview: { ready: true, state: 'CONNECTED', endpoint: '/api/webhooks/tradingview', signals_accepted: 2, signals_rejected: 1, signals_duplicate: 1 } },
    { mode: 'PAPER', market_data_provider: 'local', market_data_state: 'READY',
      market_data_availability: 'CONNECTED', execution_adapter: 'paper', risk_kill_switch: false },
  )
  assert.equal(healthy.webhook_ready, true)
  assert.equal(healthy.blockers.length, 0)
  assert.equal(healthy.signals_accepted, 2)

  const unconfigured = describeReadiness(
    { tradingview: { ready: false, state: 'NOT_CONFIGURED', reason: 'the TradingView webhook is disabled; set TRADINGVIEW_ENABLED=true' } },
    { mode: 'PAPER', market_data_availability: 'UNAVAILABLE', market_data_availability_reason: 'no quote has been observed' },
  )
  assert.equal(unconfigured.webhook_ready, false)
  assert.deepEqual(unconfigured.blockers, [
    'TradingView webhook: the TradingView webhook is disabled; set TRADINGVIEW_ENABLED=true',
    'Market data: no quote has been observed',
  ])
})

test('readiness without data reports no invented blocker', () => {
  const view = describeReadiness(null, null)
  assert.equal(view.webhook_state, 'NOT_CONFIGURED')
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

  const readiness = describeReadiness(
    connections as Record<string, unknown>,
    status as Record<string, unknown>,
  )
  assert.notEqual(readiness.mode, 'UNKNOWN')
  assert.equal(typeof readiness.webhook_ready, 'boolean')
})
