import csv
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from backend.database import SQLiteJournalRepository
from backend.portfolio import Portfolio
from backend.reporting import DailyReportingService, LocalMockNotifier, LogExportService, PerformanceService
from backend.risk.models import RiskDecision, RiskStatus
from backend.paper_trading.models import PaperTradeRecord
from backend.strategies.models import Direction, TradeSetup


DAY = date(2024, 2, 1)
BASE = datetime(2024, 1, 1, 15, tzinfo=timezone.utc)


def record_trade(repository, index, pnl, *, closed_day=DAY, symbol="ABC", strategy="fixture"):
    opened = BASE + timedelta(minutes=index)
    closed = datetime.combine(closed_day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=15, minutes=index)
    record = PaperTradeRecord(symbol, Direction.LONG, opened, 50, 47.5, 55, 1, strategy, f"ORD-{index:03d}")
    setup = TradeSetup(symbol, Direction.LONG, 50, 47.5, 55, 2.5, 5, 2, opened, strategy, .5, "test")
    repository.record_trade_open(record, setup=setup)
    exit_price = 50 + pnl
    repository.record_trade_close(record.order_id, closed, exit_price, pnl, context={"reason": "fixture"})


class JournalReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.sqlite3"
        self.repository = SQLiteJournalRepository(self.path)

    def tearDown(self):
        self.repository.close()
        self.temp.cleanup()

    def test_trade_journal_persists_reconstructable_trade_and_order_decisions(self):
        record_trade(self.repository, 1, 12.5)
        self.repository.record_event(BASE, "CYCLE_STARTED", "cycle 1", context={"cycle": 1})
        self.repository.record_error(BASE, "fixture error", symbol="abc")
        self.repository.close()
        reopened = SQLiteJournalRepository(self.path)
        try:
            trades = reopened.closed_trades(symbol="abc")
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]["pnl"], 12.5)
            self.assertEqual(trades[0]["context_json"]["setup"]["strategy_name"], "fixture")
            self.assertEqual(reopened.events()[0]["event_type"], "CYCLE_STARTED")
            self.assertEqual(reopened.errors()[0]["message"], "fixture error")
        finally:
            reopened.close()
        self.repository = SQLiteJournalRepository(self.path)

    def test_performance_daily_win_rate_and_51_percent_qualification(self):
        for index in range(49):
            record_trade(self.repository, index, 10, closed_day=DAY - timedelta(days=1))
        for index in range(49, 96):
            record_trade(self.repository, index, -5, closed_day=DAY - timedelta(days=1))
        for index, pnl in enumerate((10, 10, -5, -5), start=96):
            record_trade(self.repository, index, pnl, closed_day=DAY)
        performance = PerformanceService(self.repository, evaluation_window=100,
                                         minimum_evaluation_trades=30).calculate(as_of=DAY)
        self.assertEqual((performance.total_trades, performance.wins, performance.losses), (100, 51, 49))
        self.assertEqual(performance.win_rate, 51.0)
        self.assertTrue(performance.qualified)
        self.assertEqual(performance.qualification_status, "QUALIFIED")
        self.assertEqual(performance.daily_win_rate, 50.0)
        self.assertEqual((performance.daily_trades, performance.daily_wins, performance.daily_losses), (4, 2, 2))
        self.assertAlmostEqual(performance.total_pnl, 265)
        self.assertEqual(performance.average_win, 10)
        self.assertEqual(performance.average_loss, 5)
        self.assertIn("ABC", performance.per_symbol)
        self.assertIn("fixture", performance.per_strategy)
        self.assertIn(DAY.isoformat(), performance.daily)

    def test_below_51_percent_is_not_qualified_and_losing_trades_remain_counted(self):
        for index in range(4):
            record_trade(self.repository, index, 5 if index < 2 else -5)
        result = PerformanceService(self.repository, evaluation_window=4,
                                    minimum_evaluation_trades=4).calculate(as_of=DAY)
        self.assertEqual((result.total_trades, result.wins, result.losses), (4, 2, 2))
        self.assertEqual(result.win_rate, 50.0)
        self.assertFalse(result.qualified)
        self.assertEqual(result.qualification_status, "NOT_QUALIFIED")

    def test_daily_report_on_demand_notification_and_persistence(self):
        for index in range(4):
            record_trade(self.repository, index, 5 if index < 3 else -5)
        setup = TradeSetup("REJ", Direction.LONG, 50, 47, 56, 3, 6, 2,
                           datetime(2024, 2, 1, 14, tzinfo=timezone.utc), "fixture", .5, "test")
        self.repository.record_risk_decision(RiskDecision(RiskStatus.REJECTED, setup, 0, 0, ("risk limit",)))
        self.repository.record_error(datetime(2024, 2, 1, 14, tzinfo=timezone.utc), "paper error")
        notifier = LocalMockNotifier()
        reports = DailyReportingService(self.repository,
            PerformanceService(self.repository, evaluation_window=4, minimum_evaluation_trades=4), notifier)
        portfolio = Portfolio(10_000).snapshot()
        report = reports.generate_on_demand(DAY, portfolio, system_health="OK", notify=True)
        self.assertEqual(report.status, "REPORT")
        self.assertEqual(report.trades, 4)
        self.assertEqual(report.risk_rejections, 1)
        self.assertEqual(report.important_errors[0]["message"], "paper error")
        self.assertEqual(report.system_health, "OK")
        self.assertEqual(notifier.notify(report), "LOCAL-000002")
        self.assertEqual(len(notifier.reports), 2)
        saved = self.repository.get_daily_performance(DAY)
        self.assertEqual(saved["status"], "REPORT")

    def test_exports_support_json_csv_date_range_and_symbol(self):
        record_trade(self.repository, 1, 4, symbol="AAA")
        record_trade(self.repository, 2, -2, symbol="BBB", closed_day=DAY + timedelta(days=1))
        self.repository.record_error(datetime(2024, 2, 1, 12, tzinfo=timezone.utc), "bad bar", symbol="AAA")
        exports = LogExportService(self.repository)
        trades = json.loads(exports.export("trades", start=DAY, end=DAY, symbol="AAA"))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["symbol"], "AAA")
        errors_csv = exports.export("errors", format="csv", start=DAY, end=DAY)
        rows = list(csv.DictReader(StringIO(errors_csv)))
        self.assertEqual(rows[0]["message"], "bad bar")
        full = json.loads(exports.export("all"))
        self.assertTrue(any(row["record_type"] == "errors" for row in full))
        empty_csv = exports.export("trades", format="csv", start=DAY + timedelta(days=10))
        self.assertIn("record_type", empty_csv)
        destination = exports.write(Path(self.temp.name) / "out" / "events.json", "errors")
        self.assertTrue(destination.exists())

    def test_a_full_export_carries_the_durable_fill_journal(self):
        from backend.execution import Fill
        from backend.execution.fill_identity import normalize_fill

        fill = normalize_fill(Fill(3.0, 25.5, BASE), connection_id="paper",
                              provider_instance_id="inst-1",
                              client_order_id="ORD-1", provider_order_id="PROV-1")
        self.assertTrue(self.repository.record_provider_fill(
            fill, order_id="ORD-1", client_order_id="ORD-1", symbol="AAA",
            connection_id="paper", provider_instance_id="inst-1",
            context={"source": "pipeline", "signal_id": "sig-1"}))

        exports = LogExportService(self.repository)
        kinds = json.loads(exports.export("all"))
        recorded = [row for row in kinds if row["record_type"] == "fills"]
        self.assertEqual(len(recorded), 1, "a full export carries executions")
        self.assertEqual(recorded[0]["symbol"], "AAA")
        self.assertEqual(recorded[0]["context_json"]["source"], "pipeline")
        self.assertEqual(recorded[0]["context_json"]["signal_id"], "sig-1")

        only_fills = json.loads(exports.export("fills"))
        self.assertEqual(len(only_fills), 1)
        self.assertEqual(only_fills[0]["fill_id"], fill.fill_id)

    def test_database_failure_surfaces_to_caller(self):
        self.repository.close()
        with self.assertRaises(Exception):
            self.repository.record_event(BASE, "AFTER_CLOSE", "database failure")
        self.repository = SQLiteJournalRepository(self.path)


if __name__ == "__main__":
    unittest.main()
