from collections import defaultdict
from datetime import timezone

from backend.market_data.validation import DataValidationError, normalize_bar
from backend.strategies.models import Direction
from backend.strategies.base import Strategy
from backend.strategies.models import StrategyConfig
from backend.strategies.service import StrategyEngine

from .config import BacktestConfig
from .metrics import calculate_metrics
from .models import BacktestResult, OpenPosition, SimulatedTrade
from .sizing import position_size
from .simulation import simulate_exit


class BacktestEngine:
    def __init__(self, strategy_engine: StrategyEngine | Strategy | None = None, config: BacktestConfig | None = None,
                 strategy_config: StrategyConfig | None = None):
        self.strategy_engine = strategy_engine or StrategyEngine()
        self.strategy_config = strategy_config or StrategyConfig()
        self.config = config or BacktestConfig()

    def run(self, histories: dict[str, tuple] | dict[str, list]) -> BacktestResult:
        errors: list[str] = []
        grouped = {}
        for symbol, raw_bars in histories.items():
            try:
                bars = tuple(normalize_bar(bar) for bar in raw_bars)
                if any(bar.symbol != symbol.strip().upper() for bar in bars):
                    raise DataValidationError("history contains a different symbol")
                if any(current.timestamp <= previous.timestamp for previous, current in zip(bars, bars[1:])):
                    raise DataValidationError("bars must be strictly chronological")
                if any(bar.close <= 0 for bar in bars):
                    raise DataValidationError("prices must be positive")
                grouped[symbol.strip().upper()] = bars
            except (DataValidationError, AttributeError, TypeError) as error:
                errors.append(f"{symbol}: invalid history: {error}")

        if self.config.timezone_name not in ("US/Eastern", "America/New_York"):
            errors.append("v1 supports US Eastern trading-session filtering")

        timeline = sorted({bar.timestamp for bars in grouped.values() for bar in bars})
        bar_by_time = {symbol: {bar.timestamp: bar for bar in bars} for symbol, bars in grouped.items()}
        intents: dict[tuple[str, object], object] = {}
        for symbol, bars in grouped.items():
            prefix = []
            for index, bar in enumerate(bars[:-1]):
                prefix.append(bar)
                local_time = _eastern_time(bar.timestamp).time().replace(tzinfo=None)
                if self.config.session_open <= local_time < self.config.session_close:
                    try:
                        signal = (self.strategy_engine.evaluate(tuple(prefix))
                                  if isinstance(self.strategy_engine, StrategyEngine)
                                  else self.strategy_engine.evaluate(tuple(prefix), self.strategy_config))
                        if signal.setup is not None:
                            intents[(symbol, bars[index + 1].timestamp)] = signal.setup
                    except Exception as error:
                        errors.append(f"{symbol} {bar.timestamp.isoformat()}: strategy/state error: {error}")
        active: dict[str, OpenPosition] = {}
        completed_per_symbol: dict[str, int] = defaultdict(int)
        trades: list[SimulatedTrade] = []
        capital = self.config.starting_capital
        exposure = 0.0

        for timestamp in timeline:
            # First resolve positions carried into this bar (stop wins if both levels hit).
            for symbol in tuple(active):
                position = active[symbol]
                bar = bar_by_time[symbol].get(timestamp)
                if bar is None or timestamp <= position.entry_timestamp:
                    continue
                trade = simulate_exit(position.setup, (bar,), position.shares, self.config,
                                      entry_timestamp=position.entry_timestamp, entry_price=position.entry_price)
                if trade is not None:
                    trades.append(trade)
                    capital += trade.pnl
                    exposure -= position.shares * position.entry_price
                    completed_per_symbol[symbol] += 1
                    del active[symbol]

            for symbol, by_time in bar_by_time.items():
                bar = by_time.get(timestamp)
                if bar is None:
                    continue
                local_time = _eastern_time(timestamp).time().replace(tzinfo=None)
                in_session = self.config.session_open <= local_time < self.config.session_close

                setup = intents.get((symbol, timestamp))
                if (setup is not None and symbol not in active
                        and completed_per_symbol[symbol] < self.config.max_trades_per_symbol
                        and len(active) < self.config.max_simultaneous_positions):
                    sign = 1 if setup.direction is Direction.LONG else -1
                    entry = bar.open + sign * self.config.slippage_per_share
                    risk = abs(entry - setup.stop_price)
                    reward = abs(setup.target_price - entry)
                    ratio = reward / risk if risk else 0.0
                    if ratio < self.config.min_reward_risk or risk <= 0:
                        errors.append(f"{symbol} {timestamp.isoformat()}: next-open fill violates minimum reward/risk")
                    else:
                        # Preserve the intended stop and target; compute actual entry-based R:R.
                        setup = type(setup)(symbol=setup.symbol, direction=setup.direction,
                                            entry_price=entry, stop_price=setup.stop_price,
                                            target_price=setup.target_price, risk_per_share=risk,
                                            reward_per_share=reward, reward_risk_ratio=ratio,
                                            timestamp=setup.timestamp, strategy_name=setup.strategy_name,
                                            score=setup.score, explanation=setup.explanation)
                        shares = position_size(capital, entry, risk, self.config, current_exposure=exposure)
                        if shares:
                            active[symbol] = OpenPosition(setup, shares, timestamp, entry)
                            exposure += shares * entry
                        else:
                            # A setup without affordable size is rejected and reported as a run issue.
                            errors.append(f"{symbol} {timestamp.isoformat()}: insufficient capital or exposure capacity")

        if active:
            errors.append("backtest ended with open positions; no end-of-data liquidation price assumed")

        metrics = calculate_metrics(tuple(trades), self.config.starting_capital)
        ending_capital = self.config.starting_capital + metrics.total_pnl
        all_rr_valid = all(trade.reward_risk_ratio >= self.config.min_reward_risk for trade in trades)
        qualified = not errors and all_rr_valid and metrics.win_rate >= self.config.qualification_win_rate * 100
        return BacktestResult(tuple(trades), metrics, self.config.starting_capital, ending_capital, qualified, not errors, tuple(errors))


def _eastern_time(timestamp):
    """Convert an aware timestamp to US Eastern without an external tz database."""
    from calendar import monthcalendar, SUNDAY
    from datetime import timedelta

    utc = timestamp.astimezone(timezone.utc)
    year, month = utc.year, utc.month
    weeks = monthcalendar(year, month)
    sundays = [week[SUNDAY] for week in weeks if week[SUNDAY]]
    second_sunday_march = sundays[1]
    first_sunday_november = next(week[SUNDAY] for week in monthcalendar(year, 11) if week[SUNDAY])
    dst_start = utc.replace(month=3, day=second_sunday_march, hour=7, minute=0, second=0, microsecond=0)
    dst_end = utc.replace(month=11, day=first_sunday_november, hour=6, minute=0, second=0, microsecond=0)
    offset = timedelta(hours=-4 if dst_start <= utc < dst_end else -5)
    return utc + offset
