import { useEffect, useState } from 'react'

type Health = {
  status: string
  service: string
  environment: string
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    fetch('/api/health')
      .then((response) => {
        if (!response.ok) throw new Error('API unavailable')
        return response.json() as Promise<Health>
      })
      .then(setHealth)
      .catch(() => setError(true))
  }, [])

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand-mark">A</div>
        <span>ALPHA <b>TRADING</b></span>
        <span className="mode">FOUNDATION</span>
      </header>
      <section className="welcome">
        <p className="eyebrow">PLATFORM STATUS</p>
        <h1>Trading workspace</h1>
        <p className="subtext">The application foundation is ready. Trading features are not enabled.</p>
        <div className="status-card">
          <span className={`indicator ${health ? 'online' : ''}`} />
          <div>
            <strong>{health ? 'Backend connected' : error ? 'Backend unavailable' : 'Connecting to backend…'}</strong>
            <p>{health ? `${health.service} · ${health.environment}` : 'Status API'}</p>
          </div>
          <span className={`badge ${health ? 'good' : error ? 'bad' : ''}`}>
            {health ? 'ONLINE' : error ? 'OFFLINE' : 'CHECKING'}
          </span>
        </div>
      </section>
      <footer>ALPHA TRADING PLATFORM <span>·</span> DEVELOPMENT FOUNDATION</footer>
    </main>
  )
}
