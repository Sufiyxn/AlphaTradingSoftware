/**
 * Shared API payload types, copied from the shapes the backend actually
 * returns. Optional fields mirror fields the backend sends today; nothing
 * here is invented, and missing values stay missing (null / undefined).
 */

export type Position = {
  symbol: string
  direction: string
  quantity: number
  entry_price: number
  current_price: number | null
  price_state: string
  price_as_of: string | null
  price_age_seconds: number | null
  price_reason: string | null
  stop_price: number | null
  target_price: number | null
  unrealized_pnl: number | null
  r_multiple: number | null
  auto_exit_enabled: boolean
  status: string
}

export type PositionsPayload = {
  positions: Position[]
  portfolio: { valuation_status: string; unpriced_position_count: number }
  market_data_availability: string
  market_data_availability_reason: string | null
  valuation_status: string
  unpriced_position_count: number
}

/** The rows /api/status nests under portfolio; the trade view lives in Position. */
export type StatusPosition = {
  symbol: string
  direction?: string
  quantity?: number
  average_entry_price?: number | null
  market_price?: number | null
  opened_at?: string | null
  price_state?: string | null
  price_reason?: string | null
}

export type Status = {
  mode: string
  market_data_provider: string
  market_data_state: string
  market_data_availability: string
  market_data_availability_reason: string | null
  market_data_last_quote_at?: string | null
  market_data_stale_after_seconds?: number | null
  market_data_stream?: MarketDataStream
  valuation_status: string
  execution_adapter: string
  market_session: string
  market_trading_day: boolean
  market_session_policy: string
  market_session_orders_allowed: boolean
  owner_controls_enabled: boolean
  universe: string[]
  risk_kill_switch: boolean
  orchestrator: {
    status: string
    current_cycle: number
    last_scan_timestamp: string | null
    candidates_found: number
    candidates_rejected: number
    trades_approved: number
    trades_rejected_by_risk: number
    active_positions?: number
    session_blocked?: boolean
    session_reason?: string | null
    data_stale_rejected?: number
    interrupted_cycles?: number
    decisions_total?: number
    last_decision?: { stage: string; outcome: string; symbol: string | null } | null
  }
  portfolio: {
    cash: number
    equity: number
    available_capital?: number
    exposure: number
    realized_pnl: number
    unrealized_pnl: number
    daily_pnl?: number
    trade_count?: number
    valuation_status: string
    unpriced_position_count: number
    priced_position_count?: number
    pending_reservations?: unknown[]
    positions: StatusPosition[]
  }
}

export type Opportunity = {
  symbol: string
  direction: string
  score: number
  entry_price: number | null
  stop_price: number | null
  target_price: number | null
  reward_risk_ratio: number | null
  valid: boolean
  reasons: string[]
}

export type Trade = {
  trade_id: string
  symbol: string
  direction: string
  entry_price: number
  exit_price: number | null
  quantity: number
  pnl: number | null
  strategy: string
  entry_timestamp: string
  exit_timestamp: string | null
}

export type Performance = {
  total_trades: number
  win_rate: number
  daily_trades: number
  daily_win_rate: number
  realized_pnl: number
  daily_realized_pnl: number
  unrealized_pnl: number
  maximum_drawdown: number
  average_win: number
  average_loss: number
  profit_factor: number | null
  evaluation_trades: number
  evaluation_win_rate: number
  qualification_status: string
}

export type Log = {
  record_type: string
  timestamp?: string
  entry_timestamp?: string
  symbol?: string
  event_type?: string
  message?: string
  pnl?: number | null
}

export type TradesPayload = { trades: Trade[] }
export type ActivityPayload = { events: Log[] }

/** One observed trade price, as served by /api/quotes. */
export type Quote = {
  symbol: string
  timestamp: string | null
  price: number
  volume?: number | null
  bid?: number | null
  ask?: number | null
}

export type QuotesPayload = {
  provider: string | null
  state: string | null
  reason: string | null
  stream?: MarketDataStream | null
  quotes: Quote[]
}

/**
 * What /api/status and /api/quotes report about the live market-data socket.
 * Every field is evidence the backend recorded - state, entitlement, counters
 * - never a claim the UI upgrades on its own.
 */
export type MarketDataStream = {
  state: string
  reason: string | null
  provider?: string
  protocol?: string | null
  entitlement?: string | null
  feed?: string | null
  endpoint?: string | null
  connected_at?: string | null
  authenticated?: boolean
  subscription?: string | null
  symbols?: string[]
  last_event_at?: string | null
  last_event_symbol?: string | null
  events_received?: number
  quotes_received?: number
  ignored_messages?: number
  reconnects?: number
  attempts?: number
  last_error?: string | null
  streaming: boolean
}

/**
 * What an existing panel reports to its page through the optional onLoad
 * callback, so the page can render explicit loading/error/empty states
 * without fetching the same endpoint a second time.
 */
export type PanelLoad<T> = { data: T | null; error: string }
