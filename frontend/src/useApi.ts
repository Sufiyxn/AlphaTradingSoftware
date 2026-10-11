/**
 * The one place the UI talks to the backend.
 *
 * Every page fetches through `useApi`: it tracks loading | error | ready,
 * polls on an interval when asked, and exposes a manual refresh. The shell
 * polls /api/status through `useStatus`; control actions go through
 * `postControl`, which preserves the owner-token header contract.
 */
import { useCallback, useEffect, useState } from 'react'
import { classifyApiPhase } from './routes'
import type { ApiPhase } from './routes'
import type { Status } from './types'

export type ApiState<T> = {
  data: T | null
  error: string
  phase: ApiPhase
  refresh: () => void
}

export type UseApiOptions = {
  /** Poll interval in milliseconds; omitted means fetch once and refresh manually. */
  pollMs?: number
  /** Bump this counter to force a refetch (wired to the shell Refresh button). */
  trigger?: number
}

/** Fetch JSON and throw the backend's own error message when it fails. */
export async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(path)
  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    body = null
  }
  if (!response.ok) {
    const message =
      body !== null && typeof body === 'object' && typeof (body as { error?: unknown }).error === 'string'
        ? (body as { error: string }).error
        : ''
    throw new Error(message || `${response.status} ${response.statusText}`)
  }
  return body as T
}

/** Fetch one endpoint with loading/error/ready tracking, polling and manual refresh. */
export function useApi<T>(path: string, options: UseApiOptions = {}): ApiState<T> {
  const { pollMs, trigger = 0 } = options
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState('')
  const [tick, setTick] = useState(0)
  const refresh = useCallback(() => { setTick((value) => value + 1) }, [])

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const payload = await fetchJson<T>(path)
        if (cancelled) return
        setData(payload)
        setError('')
      } catch (cause) {
        if (cancelled) return
        setError(cause instanceof Error ? cause.message : 'API unavailable')
      }
    }
    void load()
    const timer = pollMs ? window.setInterval(() => void load(), pollMs) : undefined
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearInterval(timer)
    }
  }, [path, pollMs, tick, trigger])

  return { data, error, phase: classifyApiPhase(data !== null, error !== ''), refresh }
}

/** The shell's /api/status poller: the same hook, wired to the status endpoint. */
export function useStatus(options: UseApiOptions = {}): ApiState<Status> {
  return useApi<Status>('/api/status', { pollMs: 10000, ...options })
}

/**
 * POST an owner control action. Preserves the existing contract:
 * X-Owner-Token header, backend error text surfaced verbatim (including the
 * 503 "owner controls are disabled" refusal), accepted actions confirmed.
 */
export async function postControl(
  action: string,
  token: string,
  body: Record<string, unknown> = {},
): Promise<string> {
  const response = await fetch(`/api/control/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Owner-Token': token },
    body: JSON.stringify(body),
  })
  let result: unknown = null
  try {
    result = await response.json()
  } catch {
    result = null
  }
  if (!response.ok) {
    const message =
      result !== null && typeof result === 'object' && typeof (result as { error?: unknown }).error === 'string'
        ? (result as { error: string }).error
        : ''
    throw new Error(message || 'Control request failed')
  }
  return `${action.replaceAll('-', ' ')} accepted`
}
