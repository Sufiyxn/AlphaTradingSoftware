/**
 * The application shell: a persistent header, the hash router, the shared
 * navigation with an active state, the page heading, and the footer.
 *
 * Routing is hand-rolled (no router dependency): `routes.ts` resolves any
 * location hash into one of the nine pages, and unknown addresses get a 404
 * view that links back — never a blank screen.
 */
import { useCallback, useEffect, useState } from 'react'
import type { ComponentType } from 'react'
import { DEFAULT_ROUTE, ROUTES, documentTitleFor, parseHash, routeByPath } from './routes'
import type { PageId, ParsedRoute } from './routes'
import { useStatus } from './useApi'
import { ErrorState, LoadingState } from './components'
import type { PageProps } from './components'
import OverviewPage from './pages/OverviewPage'
import MarketsPage from './pages/MarketsPage'
import WatchlistsPage from './pages/WatchlistsPage'
import ScannerPage from './pages/ScannerPage'
import PortfolioPage from './pages/PortfolioPage'
import OrdersPage from './pages/OrdersPage'
import JournalPage from './pages/JournalPage'
import ConnectionsPage from './pages/ConnectionsPage'
import ControlPage from './pages/ControlPage'

const PAGE_COMPONENTS: Record<PageId, ComponentType<PageProps>> = {
  overview: OverviewPage,
  markets: MarketsPage,
  watchlists: WatchlistsPage,
  scanner: ScannerPage,
  portfolio: PortfolioPage,
  orders: OrdersPage,
  journal: JournalPage,
  connections: ConnectionsPage,
  control: ControlPage,
}

function useHashRoute(): ParsedRoute {
  const [route, setRoute] = useState(() => parseHash(window.location.hash))
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash))
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return route
}

function NotFoundView({ requested }: { requested: string }) {
  return <section className="panel extra-section">
    <div className="panel-head">
      <div>
        <p className="eyebrow">404 · UNKNOWN ROUTE</p>
        <h2>That page does not exist</h2>
      </div>
      <a className="button ghost" href={`#${DEFAULT_ROUTE}`}>Back to overview</a>
    </div>
    <p className="readiness-copy">
      The address <code>#{requested}</code> does not match any page of this dashboard. Pick a page below, or return to the overview.
    </p>
    <div className="log-tools">
      {ROUTES.map((item) => <a className="text-link" key={item.path} href={`#${item.path}`}>{item.nav} →</a>)}
    </div>
  </section>
}

export default function App() {
  const route = useHashRoute()
  const statusApi = useStatus()
  const [token, setToken] = useState('')
  const [chartSymbol, setChartSymbol] = useState('AAPL')
  const [refresh, setRefresh] = useState(0)
  const refreshShell = useCallback(() => {
    statusApi.refresh()
    setRefresh((value) => value + 1)
  }, [statusApi.refresh])

  useEffect(() => {
    document.title = documentTitleFor(route)
  }, [route])

  const def = route.kind === 'unknown' ? null : routeByPath(route.path)
  const Page = def ? PAGE_COMPONENTS[def.page] : null
  const pageProps: PageProps = {
    status: statusApi.data,
    statusPhase: statusApi.phase,
    statusError: statusApi.error,
    refresh,
    refreshShell,
    token,
    onTokenChange: setToken,
    chartSymbol,
    onChartSymbolChange: setChartSymbol,
  }

  return <main className="app-shell">
    <header className="topbar">
      <a className="brand" href={`#${DEFAULT_ROUTE}`}><span className="brand-icon">A</span><span>ALPHA <b>TRADING</b></span></a>
      <div className="header-state"><i className={statusApi.data ? 'online' : 'offline'} />{statusApi.error || (statusApi.data ? 'SYSTEM CONNECTED' : 'CONNECTING')}</div>
      <div className="mode-pill"><span /> PAPER MODE</div>
    </header>
    <nav className="main-nav" aria-label="Main navigation">
      {ROUTES.map((item) => (
        <a
          key={item.path}
          href={`#${item.path}`}
          className={def?.path === item.path ? 'active' : undefined}
          aria-current={def?.path === item.path ? 'page' : undefined}
        >
          {item.nav}
        </a>
      ))}
    </nav>
    <section className="page-heading">
      <div>
        <p className="eyebrow">LOCAL CONTROL CENTER <span>/</span> {new Date().toLocaleDateString()}</p>
        <h1>{def ? def.title : 'Page not found'}</h1>
        <p className="muted">{def ? def.blurb : `No page is registered for ${route.requested || 'this address'}.`}</p>
      </div>
      {def
        ? <button className="button ghost" onClick={refreshShell}>↻ <span>Refresh</span></button>
        : <a className="button ghost" href={`#${DEFAULT_ROUTE}`}>Back to overview</a>}
    </section>
    {statusApi.phase === 'error' && <ErrorState what="Backend status" message={statusApi.error} />}
    {statusApi.phase === 'loading' && <LoadingState label="Connecting to the local backend…" />}
    {Page ? <Page {...pageProps} /> : <NotFoundView requested={route.requested} />}
    <footer className="footer">
      <span>ALPHA TRADING PLATFORM <i>•</i> V1 LOCAL</span>
      <span>Read-only data · Paper execution · No broker credentials</span>
    </footer>
  </main>
}
