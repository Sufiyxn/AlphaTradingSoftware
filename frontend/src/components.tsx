/**
 * Shared presentational pieces used across the nine pages: the metric card,
 * the health row, the state badge, the empty table row, the settings field,
 * and the explicit loading/error/empty states every page renders.
 */
import type { ApiPhase } from './routes'
import type { Status } from './types'

export const usd = (n?: number | null) =>
  n == null ? '—' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n)

export const time = (s?: string | null) => (s ? new Date(s).toLocaleString() : '—')

export const age = (s?: number | null) =>
  s == null ? '—' : s < 90 ? `${Math.round(s)}s old` : s < 5400 ? `${Math.round(s / 60)}m old` : `${Math.round(s / 3600)}h old`

/** What every page receives from the app shell. Each page uses what it needs. */
export type PageProps = {
  /** The shell's latest /api/status payload, or null until the first load. */
  status: Status | null
  statusPhase: ApiPhase
  statusError: string
  /** Bumped by the shell Refresh button; pass to useApi as `trigger`. */
  refresh: number
  /** Refreshes /api/status and bumps `refresh` for every page section. */
  refreshShell: () => void
  /** The shared owner token (Connections tests and Control actions). */
  token: string
  onTokenChange: (value: string) => void
  /** The shared chart symbol, so any page can send a symbol to Markets. */
  chartSymbol: string
  onChartSymbolChange: (symbol: string) => void
}

export function Metric({ label, value, note, tone = '' }: { label: string; value: string; note: string; tone?: string }) {
  return <article className="metric-card"><p>{label}</p><strong className={tone}>{value}</strong><small>{note}</small></article>
}

export function Empty({ span, text }: { span: number; text: string }) {
  return <tr><td colSpan={span} className="empty-state">{text}</td></tr>
}

export function Health({ name, value, danger = false }: { name: string; value: string; danger?: boolean }) {
  return <div className="health-row"><span>{name}</span><b className={danger ? 'negative' : ''}>{value}</b></div>
}

export function Setting({ label, value, onChange, onSave }: { label: string; value: string; onChange: (value: string) => void; onSave: () => void }) {
  return <label className="setting-field">{label}<span><input type="number" min="1" value={value} onChange={(event) => onChange(event.target.value)} /><button className="button" onClick={onSave}>Save</button></span></label>
}

export function StateBadge({ state, reason }: { state: string; reason?: string | null }) {
  return <span className={`state-tag ${state.toLowerCase()}`} title={reason ?? undefined}>{state}</span>
}

/** LOADING: shown while a section waits for its first backend response. */
export function LoadingState({ label }: { label: string }) {
  return <div className="async-state loading" role="status"><span className="spinner" />{label}</div>
}

/** ERROR / DISCONNECTED: the message plus how to bring the backend back. */
export function ErrorState({ what, message }: { what: string; message: string }) {
  return <div className="alert danger">{what} unavailable: {message}. Start it with <code>python -m backend</code>.</div>
}

/** EMPTY: a friendly no-rows state instead of a bare table. */
export function EmptyState({ message }: { message: string }) {
  return <div className="empty-state">{message}</div>
}
