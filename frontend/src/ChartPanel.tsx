import { useEffect, useRef, useState } from 'react'
import {
  CandlestickSeries,
  CrosshairMode,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'

type Candle = { time: number; open: number; high: number; low: number; close: number; volume: number }
type SeriesPayload = {
  symbol: string
  timeframe: string
  state: string
  reason: string | null
  provider: string | null
  provider_state: string | null
  as_of: string | null
  age_seconds: number | null
  freshness: string | null
  max_age_seconds: number | null
  truncated: boolean
  bar_count: number
  timeframes: string[]
  candles: Candle[]
}

const FALLBACK_TIMEFRAMES = ['1min', '5min', '15min', '1h', '1day']
const POLL_MS = 30000

const ageLabel = (seconds: number | null) => {
  if (seconds == null) return '—'
  if (seconds < 90) return `${Math.round(seconds)}s`
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`
  if (seconds < 172800) return `${Math.round(seconds / 3600)}h`
  return `${Math.round(seconds / 86400)}d`
}

export default function ChartPanel({ symbol, onSymbolChange }: {
  symbol: string
  onSymbolChange: (symbol: string) => void
}) {
  const hostRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const fittedRef = useRef(false)
  const [timeframe, setTimeframe] = useState('1min')
  const [payload, setPayload] = useState<SeriesPayload | null>(null)
  const [error, setError] = useState('')
  const [draft, setDraft] = useState(symbol)

  useEffect(() => {
    setDraft(symbol)
  }, [symbol])

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const chart = createChart(host, {
      autoSize: true,
      layout: { background: { color: '#0d151f' }, textColor: '#8492a4' },
      grid: {
        vertLines: { color: '#1b2735' },
        horzLines: { color: '#1b2735' },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#263243' },
      timeScale: { borderColor: '#263243', timeVisible: true, secondsVisible: false },
    })
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#4bd6a3',
      downColor: '#e27e7e',
      borderUpColor: '#4bd6a3',
      borderDownColor: '#e27e7e',
      wickUpColor: '#4bd6a3',
      wickDownColor: '#e27e7e',
      priceLineColor: '#4bd6a3',
    })
    chartRef.current = chart
    seriesRef.current = series
    return () => {
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      const trimmed = symbol.trim().toUpperCase()
      if (!trimmed) {
        setPayload(null)
        setError('Enter a symbol to load candles.')
        return
      }
      const query = new URLSearchParams({ symbol: trimmed, timeframe })
      try {
        const response = await fetch(`/api/candles?${query.toString()}`)
        const body = await response.json()
        if (!response.ok) throw new Error(body.error || 'Candles are unavailable')
        if (cancelled) return
        setPayload(body as SeriesPayload)
        setError('')
      } catch (cause) {
        if (cancelled) return
        setPayload(null)
        setError(cause instanceof Error ? cause.message : 'Candles are unavailable')
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), POLL_MS)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [symbol, timeframe])

  useEffect(() => {
    const series = seriesRef.current
    if (!series) return
    if (payload && payload.state === 'OK') {
      series.setData(payload.candles.map((candle) => ({
        time: candle.time as UTCTimestamp,
        open: candle.open,
        high: candle.high,
        low: candle.low,
        close: candle.close,
      })))
      if (!fittedRef.current) {
        chartRef.current?.timeScale().fitContent()
        fittedRef.current = true
      }
    } else {
      series.setData([])
      fittedRef.current = false
    }
  }, [payload])

  const timeframes = payload?.timeframes?.length ? payload.timeframes : FALLBACK_TIMEFRAMES
  const state = payload?.state ?? 'UNKNOWN'
  return <div className="panel wide-panel chart-panel">
    <div className="panel-head">
      <div>
        <p className="eyebrow">MARKET CHART · REAL CANDLES ONLY</p>
        <h2>{symbol || 'Chart'}</h2>
      </div>
      <div className="chart-controls">
        <input
          className="chart-symbol"
          aria-label="Chart symbol"
          value={draft}
          onChange={(event) => setDraft(event.target.value.toUpperCase())}
          onKeyDown={(event) => { if (event.key === 'Enter' && draft.trim()) onSymbolChange(draft.trim().toUpperCase()) }}
          placeholder="SYMBOL"
        />
        <select
          className="chart-timeframe"
          aria-label="Chart timeframe"
          value={timeframe}
          onChange={(event) => { fittedRef.current = false; setTimeframe(event.target.value) }}
        >
          {timeframes.map((frame) => <option key={frame} value={frame}>{frame}</option>)}
        </select>
        <button className="button" disabled={!draft.trim()} onClick={() => onSymbolChange(draft.trim().toUpperCase())}>Load</button>
      </div>
    </div>
    <div className="chart-meta">
      <span className={`status-tag ${payload?.freshness === 'FRESH' ? 'good' : 'quiet'}`} title={payload?.reason ?? undefined}>
        {state}{payload?.freshness ? ` · ${payload.freshness}` : ''}{payload?.age_seconds != null ? ` · ${ageLabel(payload.age_seconds)} old` : ''}
      </span>
      <span className="count-badge">{payload?.bar_count ?? 0} candles</span>
      {payload?.truncated && <span className="status-tag quiet">TRUNCATED TO LATEST</span>}
      <span className="provider-chip">{payload?.provider ?? '—'} · {payload?.provider_state ?? '—'}</span>
    </div>
    <div className="chart-host" ref={hostRef} data-state={state} />
    {error && <p className="negative chart-note">{error}</p>}
    {!error && payload && state === 'NOT_CONFIGURED' && <p className="panel-foot chart-note">{payload.reason} · candles appear once a provider with credentials is connected in Connections &amp; Live Setup.</p>}
    {!error && payload && state === 'NO_DATA' && <p className="panel-foot chart-note">{payload.reason ?? 'No candles were returned for this symbol and window.'}</p>}
    {!error && payload && state === 'ERROR' && <p className="negative chart-note">{payload.reason ?? 'The provider could not serve this series.'}</p>}
  </div>
}
