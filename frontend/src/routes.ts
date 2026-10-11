/**
 * Pure routing and state-classification logic for the Alpha trading UI.
 *
 * Nothing in this module touches React, the DOM or the network, so Node's
 * built-in test runner can execute it directly (see `tests/routes.test.ts`)
 * and the hash router in `App.tsx` stays a thin shell around it.
 */

/** The identifier of one of the nine top-level pages. */
export type PageId =
  | 'overview'
  | 'markets'
  | 'watchlists'
  | 'scanner'
  | 'portfolio'
  | 'orders'
  | 'journal'
  | 'connections'
  | 'control'

/** One entry of the navigation: a hash path plus its visible metadata. */
export type RouteDef = {
  path: string
  page: PageId
  nav: string
  title: string
  blurb: string
}

/** The full route table. Every top-level page of the app lives here exactly once. */
export const ROUTES: readonly RouteDef[] = [
  {
    path: '/overview',
    page: 'overview',
    nav: 'Overview',
    title: 'Trading dashboard',
    blurb: 'Read-only market view with authenticated paper controls.',
  },
  {
    path: '/markets',
    page: 'markets',
    nav: 'Markets',
    title: 'Markets & live charts',
    blurb: 'Real candles and observed quotes only, with the market session and data freshness on record.',
  },
  {
    path: '/watchlists',
    page: 'watchlists',
    nav: 'Watchlists',
    title: 'Watchlists',
    blurb: 'The configured universe with live indicators from the connected market-data provider.',
  },
  {
    path: '/scanner',
    page: 'scanner',
    nav: 'Scanner',
    title: 'Scanner & strategy context',
    blurb: 'Every scanned symbol with the reason it was eligible or rejected, plus the decision-engine record.',
  },
  {
    path: '/portfolio',
    page: 'portfolio',
    nav: 'Portfolio',
    title: 'Portfolio & positions',
    blurb: 'Open paper positions with honest valuation status — unpriced stays unpriced.',
  },
  {
    path: '/orders',
    page: 'orders',
    nav: 'Orders',
    title: 'Orders & fills',
    blurb: 'Orders, fills and the signal ledger exactly as recorded in the local journal.',
  },
  {
    path: '/journal',
    page: 'journal',
    nav: 'Journal',
    title: 'Trade journal & performance',
    blurb: 'Closed trades, the persistent event journal and the measured performance.',
  },
  {
    path: '/connections',
    page: 'connections',
    nav: 'Connections',
    title: 'Connections & settings',
    blurb: 'Environment-based configuration, connectivity evidence and the safety settings.',
  },
  {
    path: '/control',
    page: 'control',
    nav: 'Control',
    title: 'Owner control center',
    blurb: 'Authenticated paper controls for the local orchestrator. Live trading stays disabled.',
  },
]

/** Where every unresolved hash lands. */
export const DEFAULT_ROUTE = '/overview'

/**
 * The single-page build used anchor ids (`#dashboard`, `#positions`, …).
 * Old bookmarks and external links keep working by normalizing onto a page.
 */
export const LEGACY_ANCHOR_ROUTES: Readonly<Record<string, string>> = {
  dashboard: '/overview',
  market: '/markets',
  markets: '/markets',
  charts: '/markets',
  watchlist: '/watchlists',
  watchlists: '/watchlists',
  scanner: '/scanner',
  decisions: '/scanner',
  positions: '/portfolio',
  portfolio: '/portfolio',
  orders: '/orders',
  performance: '/journal',
  journal: '/journal',
  backtest: '/journal',
  logs: '/journal',
  reports: '/journal',
  connections: '/connections',
  settings: '/connections',
  admin: '/control',
  control: '/control',
}

/** How a raw hash resolved: a real page, a legacy anchor, an empty hash, or junk. */
export type RouteKind = 'page' | 'legacy' | 'empty' | 'unknown'

export type ParsedRoute = {
  /** The normalized path the user asked for ('' when the hash was empty). */
  requested: string
  /** The resolved path; always a known route path. */
  path: string
  /** The query string carried by the hash, parsed and ready to read. */
  query: URLSearchParams
  kind: RouteKind
}

/** 'journal', '/journal', '/journal/' → '/journal'; '', '/', '   ' → ''. */
export function normalizePath(rawPath: string): string {
  const trimmed = (rawPath ?? '').trim()
  if (!trimmed || trimmed === '/') return ''
  const withSlash = trimmed.startsWith('/') ? trimmed : `/${trimmed}`
  return withSlash.length > 1 && withSlash.endsWith('/')
    ? withSlash.slice(0, -1)
    : withSlash
}

/** True when the path names one of the nine top-level pages. */
export function isKnownRoute(path: string): boolean {
  return ROUTES.some((route) => route.path === path)
}

/** The route definition for a resolved path; unknown paths resolve to the overview. */
export function routeByPath(path: string): RouteDef {
  const found = ROUTES.find((route) => route.path === path)
  if (found) return found
  return ROUTES.find((route) => route.path === DEFAULT_ROUTE) ?? ROUTES[0]
}

/**
 * Resolve any location hash into a page. Handles '#/journal?symbol=AAPL',
 * '#journal', 'journal', '' and legacy anchors; unknown hashes resolve to
 * the overview path and are flagged `unknown` so the UI can explain itself
 * instead of showing a blank screen.
 */
export function parseHash(hash: string): ParsedRoute {
  const withoutHash = (hash ?? '').replace(/^#/, '')
  const [rawPath, rawQuery = ''] = withoutHash.split('?')
  const query = new URLSearchParams(rawQuery)
  const requested = normalizePath(rawPath)
  if (!requested) {
    return { requested: '', path: DEFAULT_ROUTE, query, kind: 'empty' }
  }
  if (isKnownRoute(requested)) {
    return { requested, path: requested, query, kind: 'page' }
  }
  const legacy = LEGACY_ANCHOR_ROUTES[requested.slice(1).toLowerCase()]
  if (legacy && isKnownRoute(legacy)) {
    return { requested, path: legacy, query, kind: 'legacy' }
  }
  return { requested, path: DEFAULT_ROUTE, query, kind: 'unknown' }
}

/** The browser title for a parsed route; never blank, never misleading. */
export function documentTitleFor(route: ParsedRoute): string {
  if (route.kind === 'unknown') return 'Page not found · Alpha Trading'
  return `${routeByPath(route.path).title} · Alpha Trading`
}

// ---------------------------------------------------------------------------
// State classification helpers (pure, shared by every page's async plumbing)
// ---------------------------------------------------------------------------

/** The three states a page section can be in while data arrives. */
export type ApiPhase = 'loading' | 'error' | 'ready'

/**
 * Classify a fetch section: data wins (stale data plus a later error is still
 * `ready` with the error surfaced beside it); without data an error is `error`,
 * and before either arrives it is `loading`.
 */
export function classifyApiPhase(hasData: boolean, hasError: boolean): ApiPhase {
  if (hasData) return 'ready'
  return hasError ? 'error' : 'loading'
}

/** How honest a price is, using the backend's own state words. */
export type FreshnessTone = 'fresh' | 'stale' | 'unpriced'

/**
 * Map a backend price/freshness state (FRESH, STALE, UNPRICED, or anything
 * else the provider reports) onto the visual tone. Unknown stays unpriced —
 * the UI never upgrades a price it cannot vouch for.
 */
export function freshnessTone(state?: string | null): FreshnessTone {
  const normalized = (state ?? '').trim().toUpperCase()
  if (normalized === 'FRESH') return 'fresh'
  if (normalized === 'STALE') return 'stale'
  return 'unpriced'
}

export type OwnerControlsState = { available: boolean; reason: string | null }

/**
 * Decide whether the owner control buttons may be armed, and explain why not
 * when they may not. Covers both signals the backend sends: the
 * `owner_controls_enabled` flag on /api/status and the 503 body
 * "owner controls are disabled; set OWNER_CONTROL_TOKEN and restart".
 */
export function ownerControlsAvailability(
  enabled: boolean | null | undefined,
  lastError?: string | null,
): OwnerControlsState {
  if (typeof lastError === 'string' && /owner controls are disabled/i.test(lastError)) {
    return { available: false, reason: lastError }
  }
  if (enabled === false) {
    return {
      available: false,
      reason: 'Owner controls are disabled by the backend (owner_controls_enabled = false). Set OWNER_CONTROL_TOKEN in backend .env and restart it with python -m backend.',
    }
  }
  if (enabled !== true) {
    return {
      available: false,
      reason: 'The backend has not reported the owner-control state yet; controls stay locked until /api/status loads.',
    }
  }
  return { available: true, reason: null }
}
