from backend.strategies.models import Direction, TradeSetup

from .config import BacktestConfig
from .models import SimulatedTrade


def simulate_exit(setup: TradeSetup, bars, shares: int, config: BacktestConfig, *, entry_timestamp=None, entry_price=None) -> SimulatedTrade | None:
    """Use stop-first ordering when both levels occur inside one OHLC bar."""
    sign = 1 if setup.direction is Direction.LONG else -1
    entry = entry_price if entry_price is not None else setup.entry_price + sign * config.slippage_per_share
    for bar in bars:
        if bar.timestamp <= (entry_timestamp or setup.timestamp):
            continue
        stop_hit = bar.low <= setup.stop_price if sign == 1 else bar.high >= setup.stop_price
        target_hit = bar.high >= setup.target_price if sign == 1 else bar.low <= setup.target_price
        if stop_hit:
            raw_exit, reason = setup.stop_price, "stop_loss" if not target_hit else "stop_and_target_same_bar_stop_first"
            break
        if target_hit:
            raw_exit, reason = setup.target_price, "take_profit"
            break
    else:
        return None

    exit_price = raw_exit - sign * config.slippage_per_share
    fees = shares * config.commission_per_share * 2
    gross = (exit_price - entry) * sign * shares
    pnl = gross - fees
    entry_notional = entry * shares
    risk = abs(setup.entry_price - setup.stop_price)
    reward = abs(setup.target_price - setup.entry_price)
    return SimulatedTrade(
        symbol=setup.symbol,
        direction=setup.direction,
        entry_timestamp=entry_timestamp or next(bar.timestamp for bar in bars if bar.timestamp > setup.timestamp),
        entry_price=entry,
        stop_price=setup.stop_price,
        target_price=setup.target_price,
        exit_timestamp=bar.timestamp,
        exit_price=exit_price,
        risk_per_share=risk,
        reward_per_share=reward,
        reward_risk_ratio=reward / risk if risk else 0.0,
        pnl=pnl,
        pnl_percent=pnl / entry_notional * 100 if entry_notional else 0.0,
        won=pnl > 0,
        strategy_name=setup.strategy_name,
        exit_reason=reason,
        shares=shares,
        fees=fees,
    )
