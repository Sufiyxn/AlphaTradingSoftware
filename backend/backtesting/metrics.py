from collections import defaultdict

from .models import PerformanceMetrics, SimulatedTrade


def calculate_metrics(trades: tuple[SimulatedTrade, ...], starting_capital: float) -> PerformanceMetrics:
    wins = [trade for trade in trades if trade.won]
    losses = [trade for trade in trades if not trade.won]
    total_pnl = sum(trade.pnl for trade in trades)
    gross_profit = sum(max(0.0, trade.pnl) for trade in trades)
    gross_loss = abs(sum(min(0.0, trade.pnl) for trade in trades))

    equity = starting_capital
    peak = equity
    max_drawdown = 0.0
    for trade in trades:
        equity += trade.pnl
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)

    by_symbol: dict[str, list[SimulatedTrade]] = defaultdict(list)
    for trade in trades:
        by_symbol[trade.symbol].append(trade)
    streak_wins = streak_losses = max_wins = max_losses = 0
    for trade in trades:
        if trade.won:
            streak_wins += 1
            streak_losses = 0
            max_wins = max(max_wins, streak_wins)
        else:
            streak_losses += 1
            streak_wins = 0
            max_losses = max(max_losses, streak_losses)

    count = len(trades)
    return PerformanceMetrics(
        total_trades=count,
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate=len(wins) / count * 100 if count else 0.0,
        total_pnl=total_pnl,
        total_return_pct=total_pnl / starting_capital * 100 if starting_capital else 0.0,
        average_win=sum(t.pnl for t in wins) / len(wins) if wins else 0.0,
        average_loss=sum(t.pnl for t in losses) / len(losses) if losses else 0.0,
        average_reward_risk=sum(t.reward_risk_ratio for t in trades) / count if count else 0.0,
        profit_factor=gross_profit / gross_loss if gross_loss else (float("inf") if gross_profit else 0.0),
        maximum_drawdown=max_drawdown,
        largest_win=max((t.pnl for t in trades), default=0.0),
        largest_loss=min((t.pnl for t in trades), default=0.0),
        consecutive_wins=max_wins,
        consecutive_losses=max_losses,
        trades_per_symbol={symbol: len(items) for symbol, items in sorted(by_symbol.items())},
        win_rate_per_symbol={symbol: sum(t.won for t in items) / len(items) * 100 for symbol, items in sorted(by_symbol.items())},
        pnl_per_symbol={symbol: sum(t.pnl for t in items) for symbol, items in sorted(by_symbol.items())},
    )
