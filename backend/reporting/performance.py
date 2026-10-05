from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from backend.database import JournalRepository
from backend.portfolio.models import PortfolioSnapshot


@dataclass(frozen=True, slots=True)
class GroupPerformance:
    trades: int
    wins: int
    losses: int
    win_rate: float
    realized_pnl: float


@dataclass(frozen=True, slots=True)
class PerformanceSnapshot:
    as_of: date
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    realized_pnl: float
    unrealized_pnl: float
    average_win: float
    average_loss: float
    profit_factor: float | None
    maximum_drawdown: float
    maximum_drawdown_pct: float
    average_reward_risk: float
    minimum_reward_risk: float | None
    maximum_reward_risk: float | None
    evaluation_window_size: int
    evaluation_trades: int
    evaluation_win_rate: float
    qualified: bool
    qualification_status: str
    daily_trades: int
    daily_wins: int
    daily_losses: int
    daily_win_rate: float
    daily_realized_pnl: float
    per_symbol: dict[str, GroupPerformance]
    per_strategy: dict[str, GroupPerformance]
    daily: dict[str, GroupPerformance]
    daily_per_strategy: dict[str, GroupPerformance]


class PerformanceService:
    def __init__(self, repository: JournalRepository, *, minimum_win_rate: float = 51.0,
                 evaluation_window: int = 100, minimum_evaluation_trades: int = 30,
                 starting_equity: float = 100_000.0):
        if not 51.0 <= minimum_win_rate <= 100 or evaluation_window < 1 or not 1 <= minimum_evaluation_trades <= evaluation_window:
            raise ValueError("invalid performance qualification settings")
        if starting_equity <= 0:
            raise ValueError("starting equity must be positive")
        self.repository = repository
        self.minimum_win_rate = minimum_win_rate
        self.evaluation_window = evaluation_window
        self.minimum_evaluation_trades = minimum_evaluation_trades
        self.starting_equity = starting_equity

    def calculate(self, *, as_of: date, portfolio: PortfolioSnapshot | None = None) -> PerformanceSnapshot:
        rows = self.repository.closed_trades()
        ordered = sorted(rows, key=lambda row: (row["exit_timestamp"], row["trade_id"]))
        pnls = [float(row["pnl"] or 0.0) for row in ordered]
        wins = sum(pnl > 0 for pnl in pnls)
        losses = sum(pnl < 0 for pnl in pnls)
        count = len(pnls)
        win_rate = wins / count * 100 if count else 0.0
        positive = [pnl for pnl in pnls if pnl > 0]
        negative = [-pnl for pnl in pnls if pnl < 0]
        gross_win, gross_loss = sum(positive), sum(negative)
        cumulative = peak = drawdown = 0.0
        for pnl in pnls:
            cumulative += pnl
            peak = max(peak, cumulative)
            drawdown = max(drawdown, peak - cumulative)
        drawdown_pct = drawdown / self.starting_equity * 100
        ratios = [float(row["reward_risk_ratio"]) for row in ordered if row["reward_risk_ratio"] is not None]
        window = ordered[-self.evaluation_window:]
        window_wins = sum(float(row["pnl"] or 0) > 0 for row in window)
        evaluation_rate = window_wins / len(window) * 100 if window else 0.0
        qualified = len(window) >= self.minimum_evaluation_trades and evaluation_rate >= self.minimum_win_rate
        daily_rows = [row for row in ordered if row["exit_timestamp"][:10] == as_of.isoformat()]
        daily_wins = sum(float(row["pnl"] or 0) > 0 for row in daily_rows)
        daily_losses = sum(float(row["pnl"] or 0) < 0 for row in daily_rows)
        by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_daily_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in ordered:
            by_symbol[row["symbol"]].append(row)
            by_strategy[row["strategy"]].append(row)
            by_day[row["exit_timestamp"][:10]].append(row)
            if row["exit_timestamp"][:10] == as_of.isoformat():
                by_daily_strategy[row["strategy"]].append(row)
        snapshot = portfolio
        realized = sum(pnls)
        unrealized = snapshot.unrealized_pnl if snapshot else 0.0
        daily_realized = sum(float(row["pnl"] or 0) for row in daily_rows)
        return PerformanceSnapshot(
            as_of=as_of, total_trades=count, wins=wins, losses=losses, win_rate=win_rate,
            total_pnl=realized + unrealized, realized_pnl=realized, unrealized_pnl=unrealized,
            average_win=sum(positive) / len(positive) if positive else 0.0,
            average_loss=sum(negative) / len(negative) if negative else 0.0,
            profit_factor=gross_win / gross_loss if gross_loss else None,
            maximum_drawdown=drawdown, maximum_drawdown_pct=drawdown_pct,
            average_reward_risk=sum(ratios) / len(ratios) if ratios else 0.0,
            minimum_reward_risk=min(ratios) if ratios else None,
            maximum_reward_risk=max(ratios) if ratios else None,
            evaluation_window_size=self.evaluation_window, evaluation_trades=len(window),
            evaluation_win_rate=evaluation_rate, qualified=qualified,
            qualification_status="QUALIFIED" if qualified else "NOT_QUALIFIED",
            daily_trades=len(daily_rows), daily_wins=daily_wins, daily_losses=daily_losses,
            daily_win_rate=daily_wins / len(daily_rows) * 100 if daily_rows else 0.0,
            daily_realized_pnl=daily_realized,
            per_symbol={key: _group(items) for key, items in sorted(by_symbol.items())},
            per_strategy={key: _group(items) for key, items in sorted(by_strategy.items())},
            daily={key: _group(items) for key, items in sorted(by_day.items())},
            daily_per_strategy={key: _group(items) for key, items in sorted(by_daily_strategy.items())},
        )


def _group(rows: list[dict[str, Any]]) -> GroupPerformance:
    pnls = [float(row["pnl"] or 0) for row in rows]
    wins = sum(pnl > 0 for pnl in pnls)
    losses = sum(pnl < 0 for pnl in pnls)
    return GroupPerformance(len(rows), wins, losses, wins / len(rows) * 100 if rows else 0.0, sum(pnls))
