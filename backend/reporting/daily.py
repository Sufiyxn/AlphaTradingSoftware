from dataclasses import dataclass
from datetime import date
from typing import Protocol

from backend.database import JournalRepository
from backend.portfolio.models import PortfolioSnapshot

from .performance import PerformanceService, PerformanceSnapshot


@dataclass(frozen=True, slots=True)
class DailyReport:
    report_date: date
    status: str
    trades: int
    wins: int
    losses: int
    daily_win_rate: float
    realized_pnl: float
    unrealized_pnl: float
    exposure: float
    active_positions: tuple[object, ...]
    strategy_performance: dict
    risk_rejections: int
    important_errors: tuple[dict, ...]
    system_health: str
    qualification_status: str
    evaluation_window_trades: int
    evaluation_window_win_rate: float
    performance: PerformanceSnapshot


class NotificationProvider(Protocol):
    def notify(self, report: DailyReport) -> str: ...


class LocalMockNotifier:
    """In-process sink for deterministic reporting tests; no delivery channel."""

    def __init__(self):
        self.reports: list[DailyReport] = []

    def notify(self, report: DailyReport) -> str:
        self.reports.append(report)
        return f"LOCAL-{len(self.reports):06d}"


class DailyReportingService:
    def __init__(self, repository: JournalRepository, performance: PerformanceService,
                 notifier: NotificationProvider | None = None):
        self.repository = repository
        self.performance = performance
        self.notifier = notifier or LocalMockNotifier()

    def generate(self, report_date: date, portfolio: PortfolioSnapshot, *, system_health: str = "OK") -> DailyReport:
        performance = self.performance.calculate(as_of=report_date, portfolio=portfolio)
        risk_decisions = self.repository.rows("risk_decisions", start=report_date, end=report_date)
        rejected = sum(not bool(item["approved"]) for item in risk_decisions)
        errors = self.repository.errors(start=report_date, end=report_date, limit=100)
        report = DailyReport(
            report_date, "REPORT" if performance.qualified else "NOT_QUALIFIED",
            performance.daily_trades, performance.daily_wins, performance.daily_losses,
            performance.daily_win_rate, performance.daily_realized_pnl, performance.unrealized_pnl,
            portfolio.exposure, portfolio.positions, performance.daily_per_strategy, rejected, errors,
            system_health, performance.qualification_status, performance.evaluation_trades,
            performance.evaluation_win_rate, performance,
        )
        self.repository.save_daily_performance(report_date, report)
        return report

    def generate_on_demand(self, report_date: date, portfolio: PortfolioSnapshot,
                           *, system_health: str = "OK", notify: bool = False) -> DailyReport:
        report = self.generate(report_date, portfolio, system_health=system_health)
        if notify:
            self.notifier.notify(report)
        return report
