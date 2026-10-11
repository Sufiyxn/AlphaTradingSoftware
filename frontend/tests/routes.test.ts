/**
 * Routing and state-classification tests, run with Node's built-in runner:
 *
 *     node --test tests/
 *
 * They exercise `src/routes.ts`, the pure module behind the hash router in
 * `App.tsx`: the route table, hash parsing/normalization, the unknown-route
 * fallback, and the classification helpers every page's async plumbing uses.
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  DEFAULT_ROUTE,
  LEGACY_ANCHOR_ROUTES,
  ROUTES,
  classifyApiPhase,
  documentTitleFor,
  freshnessTone,
  isKnownRoute,
  normalizePath,
  ownerControlsAvailability,
  parseHash,
  routeByPath,
} from '../src/routes.ts'

const REQUIRED_PATHS = [
  '/connections',
  '/control',
  '/journal',
  '/markets',
  '/orders',
  '/overview',
  '/portfolio',
  '/scanner',
  '/watchlists',
]

test('every required page maps to a route entry', () => {
  assert.deepEqual(ROUTES.map((route) => route.path).sort(), REQUIRED_PATHS)
  assert.equal(new Set(ROUTES.map((route) => route.path)).size, ROUTES.length, 'no duplicate paths')
  for (const route of ROUTES) {
    assert.equal(isKnownRoute(route.path), true, `${route.path} is a known route`)
    assert.equal(routeByPath(route.path).page, route.page)
    assert.notEqual(route.nav, '')
    assert.notEqual(route.title, '')
    assert.notEqual(route.blurb, '')
  }
})

test('route pages use the nine distinct page ids', () => {
  const pages = new Set(ROUTES.map((route) => route.page))
  assert.equal(pages.size, 9)
})

test('parseHash resolves every required route to itself', () => {
  for (const path of REQUIRED_PATHS) {
    const parsed = parseHash(`#${path}`)
    assert.equal(parsed.kind, 'page', `${path} parses as a page`)
    assert.equal(parsed.path, path)
    assert.equal(parsed.requested, path)
  }
})

test('parseHash reads query strings off the hash', () => {
  const parsed = parseHash('#/journal?symbol=AAPL&start=2024-01-01')
  assert.equal(parsed.kind, 'page')
  assert.equal(parsed.path, '/journal')
  assert.equal(parsed.query.get('symbol'), 'AAPL')
  assert.equal(parsed.query.get('start'), '2024-01-01')
  assert.equal(parseHash('#/markets?x=1&x=2').query.getAll('x').length, 2)
})

test('empty and bare hashes fall back to the overview page', () => {
  for (const hash of ['', '#', '#/', '#?page=1']) {
    const parsed = parseHash(hash)
    assert.equal(parsed.kind, 'empty', `${JSON.stringify(hash)} is an empty hash`)
    assert.equal(parsed.path, DEFAULT_ROUTE)
  }
  assert.equal(parseHash('#?page=1').query.get('page'), '1')
})

test('unknown routes resolve to the overview path and are flagged', () => {
  for (const hash of ['#/does-not-exist', '#/overview/extra', '#what', '#/SETTINGS/deep']) {
    const parsed = parseHash(hash)
    assert.equal(parsed.path, DEFAULT_ROUTE, `${hash} falls back to overview`)
    assert.notEqual(parsed.kind, 'page', `${hash} is not presented as a real page`)
  }
  assert.equal(parseHash('#/does-not-exist').kind, 'unknown')
  assert.equal(parseHash('#/does-not-exist').requested, '/does-not-exist')
})

test('legacy single-page anchors normalize onto the new pages', () => {
  const cases: [string, string][] = [
    ['#dashboard', '/overview'],
    ['#/dashboard', '/overview'],
    ['#market', '/markets'],
    ['#charts', '/markets'],
    ['#watchlist', '/watchlists'],
    ['#scanner', '/scanner'],
    ['#decisions', '/scanner'],
    ['#positions', '/portfolio'],
    ['#orders', '/orders'],
    ['#performance', '/journal'],
    ['#backtest', '/journal'],
    ['#logs', '/journal'],
    ['#reports', '/journal'],
    ['#connections', '/connections'],
    ['#settings', '/connections'],
    ['#admin', '/control'],
  ]
  for (const [hash, expected] of cases) {
    const parsed = parseHash(hash)
    assert.equal(parsed.path, expected, `${hash} → ${expected}`)
    assert.ok(parsed.kind === 'legacy' || parsed.kind === 'page')
    assert.equal(isKnownRoute(parsed.path), true)
  }
  for (const [anchor, target] of Object.entries(LEGACY_ANCHOR_ROUTES)) {
    assert.equal(isKnownRoute(target), true, `legacy anchor ${anchor} targets a real page`)
  }
})

test('normalizePath trims hashes down to a clean path', () => {
  assert.equal(normalizePath(''), '')
  assert.equal(normalizePath('/'), '')
  assert.equal(normalizePath('  /  '), '')
  assert.equal(normalizePath('journal'), '/journal')
  assert.equal(normalizePath('/journal'), '/journal')
  assert.equal(normalizePath('/journal/'), '/journal')
})

test('routeByPath falls back to the overview for unknown paths', () => {
  assert.equal(routeByPath('/scanner').page, 'scanner')
  assert.equal(routeByPath('/nonsense').path, DEFAULT_ROUTE)
  assert.equal(routeByPath('').page, routeByPath(DEFAULT_ROUTE).page)
})

test('document titles change per page and explain a 404', () => {
  assert.equal(documentTitleFor(parseHash('#/markets')), 'Markets & live charts · Alpha Trading')
  assert.equal(documentTitleFor(parseHash('#/control')), 'Owner control center · Alpha Trading')
  assert.equal(documentTitleFor(parseHash('')), 'Trading dashboard · Alpha Trading')
  assert.equal(documentTitleFor(parseHash('#/nowhere')), 'Page not found · Alpha Trading')
})

test('classifyApiPhase separates loading, error and ready', () => {
  assert.equal(classifyApiPhase(false, false), 'loading', 'first fetch in flight')
  assert.equal(classifyApiPhase(false, true), 'error', 'failed before any data')
  assert.equal(classifyApiPhase(true, false), 'ready')
  assert.equal(classifyApiPhase(true, true), 'ready', 'stale data stays ready with the error shown beside it')
})

test('freshnessTone never upgrades a price it cannot vouch for', () => {
  assert.equal(freshnessTone('FRESH'), 'fresh')
  assert.equal(freshnessTone('fresh'), 'fresh')
  assert.equal(freshnessTone('STALE'), 'stale')
  assert.equal(freshnessTone('UNPRICED'), 'unpriced')
  assert.equal(freshnessTone('SOMETHING_NEW'), 'unpriced')
  assert.equal(freshnessTone(null), 'unpriced')
  assert.equal(freshnessTone(undefined), 'unpriced')
  assert.equal(freshnessTone('  '), 'unpriced')
})

test('ownerControlsAvailability explains both refusal signals', () => {
  assert.deepEqual(ownerControlsAvailability(true), { available: true, reason: null })

  const disabled = ownerControlsAvailability(false)
  assert.equal(disabled.available, false)
  assert.match(disabled.reason ?? '', /owner_controls_enabled = false/)
  assert.match(disabled.reason ?? '', /OWNER_CONTROL_TOKEN/)

  const backendRefusal = ownerControlsAvailability(
    true,
    'owner controls are disabled; set OWNER_CONTROL_TOKEN and restart',
  )
  assert.equal(backendRefusal.available, false)
  assert.equal(backendRefusal.reason, 'owner controls are disabled; set OWNER_CONTROL_TOKEN and restart')

  const notLoaded = ownerControlsAvailability(undefined)
  assert.equal(notLoaded.available, false)
  assert.match(notLoaded.reason ?? '', /has not reported/)
})
