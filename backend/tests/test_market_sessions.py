"""Phase 11 checkpoint 1: the market-session gate.

Two layers are proven here:

- the calendar/gate model in ``backend.market_data.session`` answers every
  boundary the platform depends on (regular-session edges, weekends, market
  holidays, half days, daylight-saving changes, the two submission policies,
  and the provider's extended-hours capability);
- the execution pipeline actually enforces that answer: a refused submission
  carries ``PipelineCode.SESSION_BLOCKED``, creates no order, and the same
  intent succeeds once the exchange reopens.

Nothing here reaches the network. Every instant is expressed in UTC and
checked against exchange-local behaviour.
"""
import unittest
from datetime import datetime, timezone

from backend.config import load_config
from backend.market_data.session import (
    DEFAULT_SESSION_POLICY,
    EXTENDED_HOURS_UNSUPPORTED,
    MARKET_CLOSED,
    OUTSIDE_ALLOWED_WINDOW,
    SESSION_OK,
    SESSION_POLICIES,
    UNKNOWN_ASSET_CLASS,
    AssetClass,
    asset_class_for_symbol,
    market_session,
    normalize_session_policy,
    session_gate,
)
from backend.pipeline import ExecutionIntent, PipelineCode
from backend.portfolio import Portfolio
from backend.tests.test_pipeline import SpyAdapter, SpyRisk, make_manager


def utc(*args) -> datetime:
    """Build a UTC instant: ``utc(2026, 10, 9, 15, 0)`` = Friday 11:00 ET."""
    return datetime(*args, tzinfo=timezone.utc)


def pipeline_for(at, *, session_submission="regular", adapter=None):
    clock = lambda: at  # noqa: E731 - deterministic fixture clock
    adapter = adapter if adapter is not None else SpyAdapter(clock=clock)
    risk = SpyRisk()
    manager = make_manager(adapter, risk=risk)
    from backend.pipeline import TradingPipeline
    pipeline = TradingPipeline(
        connections=manager, risk_engine=risk, portfolio=Portfolio(100_000),
        clock=clock, connection_id="paper",
        session_submission=session_submission)
    return pipeline, adapter, risk


def intent_for(at, *, symbol="AAPL"):
    from backend.strategies.models import Direction, TradeSetup
    setup = TradeSetup(symbol, Direction.LONG, 50.0, 49.0, 52.0, 1.0, 2.0, 2.0,
                       at, "session-proof", .5, "deterministic")
    return ExecutionIntent(setup=setup, source="session-test",
                           occurred_at=at, max_age_seconds=900.0)


class SessionGateCalendarTests(unittest.TestCase):
    """The calendar half of the gate, independent of any pipeline."""

    def test_regular_session_boundaries(self):
        # Thursday 2026-10-08, EDT (UTC-4).
        decision = session_gate(utc(2026, 10, 8, 13, 29, 59))
        self.assertFalse(decision.allowed, "one second before the open")
        self.assertEqual(decision.code, OUTSIDE_ALLOWED_WINDOW)
        self.assertEqual(decision.state, "PRE_MARKET")

        decision = session_gate(utc(2026, 10, 8, 13, 30, 0))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.code, SESSION_OK)
        self.assertEqual(decision.state, "REGULAR")

        decision = session_gate(utc(2026, 10, 8, 19, 59, 59))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.state, "REGULAR")

        decision = session_gate(utc(2026, 10, 8, 20, 0, 0))
        self.assertFalse(decision.allowed, "exactly the regular close")
        self.assertEqual(decision.state, "AFTER_HOURS")
        self.assertEqual(decision.code, OUTSIDE_ALLOWED_WINDOW)

    def test_regular_session_boundaries_across_the_dst_change(self):
        # EST (UTC-5): the market opens at 14:30Z.
        self.assertEqual(session_gate(utc(2026, 1, 5, 14, 29, 59)).code,
                         OUTSIDE_ALLOWED_WINDOW)
        self.assertEqual(session_gate(utc(2026, 1, 5, 14, 30)).code, SESSION_OK)
        # EDT (UTC-4): the market opens at 13:30Z.
        self.assertEqual(session_gate(utc(2026, 7, 6, 13, 29, 59)).code,
                         OUTSIDE_ALLOWED_WINDOW)
        self.assertEqual(session_gate(utc(2026, 7, 6, 13, 30)).code, SESSION_OK)
        # The close moves with it: 21:00Z in winter (16:00 EST), 20:00Z in
        # summer (16:00 EDT).
        self.assertEqual(session_gate(utc(2026, 1, 5, 20, 59, 59)).state, "REGULAR")
        self.assertEqual(session_gate(utc(2026, 1, 5, 21, 0)).state, "AFTER_HOURS")
        self.assertEqual(session_gate(utc(2026, 7, 6, 19, 59, 59)).state, "REGULAR")
        self.assertEqual(session_gate(utc(2026, 7, 6, 20, 0)).state, "AFTER_HOURS")

    def test_weekend_is_closed_under_both_policies(self):
        for policy in SESSION_POLICIES:
            with self.subTest(policy=policy):
                decision = session_gate(utc(2026, 10, 10, 15, 0), policy=policy)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.code, MARKET_CLOSED)
                self.assertEqual(decision.trading_date.isoformat(), "2026-10-10")
                self.assertIn("2026-10-12", decision.reason,
                              "the resume text must name the next trading day")

    def test_market_holidays_are_closed(self):
        holidays = {
            "2026-01-19": "Martin Luther King Jr. Day",
            "2026-04-03": "Good Friday",
            "2026-05-25": "Memorial Day",
            "2026-06-19": "Juneteenth",
            "2026-07-03": "Independence Day observed",
            "2026-09-07": "Labor Day",
            "2026-11-26": "Thanksgiving",
            "2026-12-25": "Christmas Day",
        }
        for day, name in holidays.items():
            with self.subTest(holiday=name):
                year, month, dom = (int(part) for part in day.split("-"))
                at = datetime(year, month, dom, 15, 0, tzinfo=timezone.utc)
                decision = session_gate(at)
                self.assertFalse(decision.allowed, name)
                self.assertEqual(decision.code, MARKET_CLOSED, name)
                self.assertFalse(decision.is_trading_day, name)

    def test_observed_holiday_moves_a_saturday_onto_the_friday_before(self):
        # New Year's Day 2022 fell on a Saturday, so it was observed on
        # Friday 2021-12-31 - and Monday 2022-01-03 stayed a trading day.
        closed = session_gate(utc(2021, 12, 31, 15, 0))
        self.assertEqual(closed.code, MARKET_CLOSED)
        opened = session_gate(utc(2022, 1, 3, 15, 0))
        self.assertEqual(opened.code, SESSION_OK)
        # A Sunday holiday is observed on the following Monday: Christmas
        # 2022 fell on a Sunday, so Monday 2022-12-26 was the holiday.
        self.assertEqual(session_gate(utc(2022, 12, 26, 15, 0)).code, MARKET_CLOSED)

    def test_half_day_closes_early(self):
        # 2026-11-27 (day after Thanksgiving) closes at 13:00 ET.
        session = market_session(utc(2026, 11, 27, 12, 0))
        self.assertEqual(session.regular_close.strftime("%H:%M:%S"), "13:00:00")
        self.assertEqual(session_gate(utc(2026, 11, 27, 17, 59, 59)).code,
                         SESSION_OK, "12:59:59 ET is still regular")
        self.assertEqual(session_gate(utc(2026, 11, 27, 18, 0)).state,
                         "AFTER_HOURS", "13:00 ET is already after-hours")

    def test_naive_instants_are_read_as_utc_not_host_local(self):
        naive = utc(2026, 10, 8, 13, 30).replace(tzinfo=None)
        self.assertEqual(session_gate(naive).code, SESSION_OK)

    def test_asset_classes(self):
        self.assertEqual(asset_class_for_symbol("AAPL"), AssetClass.US_EQUITY)
        decision = session_gate(utc(2026, 10, 8, 15, 0), asset_class="CRYPTO")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, UNKNOWN_ASSET_CLASS)
        self.assertIn("no market calendar", decision.reason)

    def test_default_policy_is_regular(self):
        self.assertEqual(DEFAULT_SESSION_POLICY, "regular")
        self.assertEqual(normalize_session_policy(None), "regular")
        self.assertEqual(normalize_session_policy("EXTENDED"), "extended")
        with self.assertRaises(ValueError):
            normalize_session_policy("24x7")


class SessionGatePolicyTests(unittest.TestCase):
    """The two submission policies against pre- and after-hours."""

    PRE = utc(2026, 10, 8, 12, 0)      # 08:00 ET, pre-market
    AFTER = utc(2026, 10, 8, 21, 0)    # 17:00 ET, after-hours

    def test_regular_policy_refuses_everything_outside_the_session(self):
        for at in (self.PRE, self.AFTER):
            with self.subTest(at=at.isoformat()):
                decision = session_gate(at, policy="regular")
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.code, OUTSIDE_ALLOWED_WINDOW)
                self.assertIn("regular session", decision.reason)

    def test_extended_policy_allows_only_within_the_platform_window(self):
        # 04:00 ET = 08:00Z (EDT). Just before it is still refused.
        self.assertEqual(session_gate(utc(2026, 10, 8, 7, 59), policy="extended").code,
                         OUTSIDE_ALLOWED_WINDOW)
        self.assertEqual(session_gate(utc(2026, 10, 8, 8, 0), policy="extended").code,
                         SESSION_OK)
        self.assertEqual(session_gate(self.PRE, policy="extended").code, SESSION_OK)
        self.assertEqual(session_gate(self.AFTER, policy="extended").code, SESSION_OK)
        # 20:00 ET = 00:00Z the next day - outside the window.
        self.assertEqual(
            session_gate(utc(2026, 10, 9, 0, 0), policy="extended").code,
            OUTSIDE_ALLOWED_WINDOW)

    def test_extended_policy_still_respects_the_calendar_and_capability(self):
        weekend = utc(2026, 10, 10, 12, 0)  # Saturday 08:00 ET
        decision = session_gate(weekend, policy="extended")
        self.assertEqual(decision.code, MARKET_CLOSED, "a holiday is never open")
        decision = session_gate(self.PRE, policy="extended",
                                provider_extended_hours=False)
        self.assertEqual(decision.code, EXTENDED_HOURS_UNSUPPORTED)
        self.assertIn("does not support extended-hours", decision.reason)

    def test_provider_capability_never_unlocks_the_regular_policy(self):
        decision = session_gate(self.PRE, policy="regular",
                                provider_extended_hours=True)
        self.assertFalse(decision.allowed)


class PipelineSessionGateTests(unittest.TestCase):
    """The enforcement half: the pipeline refuses, then allows, the same intent."""

    def test_submission_outside_the_session_is_blocked_and_no_order_exists(self):
        saturday = utc(2026, 10, 10, 15, 0)
        pipeline, adapter, _ = pipeline_for(saturday)
        result = pipeline.execute(intent_for(saturday))
        self.assertEqual(result.code, PipelineCode.SESSION_BLOCKED)
        self.assertFalse(result.accepted)
        self.assertIsNone(result.order)
        self.assertFalse(adapter.requests,
                         "the adapter must never see a closed-session order")
        self.assertEqual(pipeline.orders.list(), (),
                         "a refused submission creates no order anywhere")

    def test_the_same_intent_succeeds_once_the_exchange_reopens(self):
        saturday = utc(2026, 10, 10, 15, 0)
        pipeline, adapter, _ = pipeline_for(saturday)
        self.assertEqual(pipeline.execute(intent_for(saturday)).code,
                         PipelineCode.SESSION_BLOCKED)

        monday_open = utc(2026, 10, 12, 14, 0)  # Monday 10:00 ET
        pipeline, adapter, _ = pipeline_for(monday_open)
        result = pipeline.execute(intent_for(monday_open))
        self.assertEqual(result.code, PipelineCode.ACCEPTED)
        self.assertIsNotNone(result.order)
        self.assertEqual(len(adapter.requests), 1)

    def test_pre_market_is_blocked_then_allowed_under_the_extended_policy(self):
        wednesday_0800 = utc(2026, 10, 7, 12, 0)  # 08:00 ET
        pipeline, adapter, _ = pipeline_for(wednesday_0800)
        result = pipeline.execute(intent_for(wednesday_0800))
        self.assertEqual(result.code, PipelineCode.SESSION_BLOCKED)

        pipeline, adapter, _ = pipeline_for(wednesday_0800,
                                            session_submission="extended")
        result = pipeline.execute(intent_for(wednesday_0800))
        self.assertEqual(result.code, PipelineCode.ACCEPTED)
        self.assertEqual(len(adapter.requests), 1)

    def test_the_block_is_journaled_with_its_own_code(self):
        from backend.database import SQLiteJournalRepository
        saturday = utc(2026, 10, 10, 15, 0)
        journal = SQLiteJournalRepository(":memory:")
        try:
            pipeline, _, _ = pipeline_for(saturday)
            pipeline.journal = journal
            result = pipeline.execute(intent_for(saturday))
            self.assertEqual(result.code, PipelineCode.SESSION_BLOCKED)
            blocked = [event for event in journal.events()
                       if event["context_json"].get("code")
                       == PipelineCode.SESSION_BLOCKED]
            self.assertTrue(blocked, "the refusal must be journaled")
            reason = blocked[0]["context_json"].get("reason") or ""
            self.assertIn("closed", reason.lower())
        finally:
            journal.close()

    def test_an_unknown_policy_cannot_reach_the_pipeline(self):
        with self.assertRaises(ValueError):
            pipeline_for(utc(2026, 10, 8, 15, 0), session_submission="whenever")


class SessionConfigurationTests(unittest.TestCase):
    """The setting itself: defaults, parsing, and refusal of nonsense."""

    def test_default_is_the_regular_session(self):
        config = load_config(env={"TRADING_MODE": "PAPER"})
        self.assertEqual(config.trading.session_submission, "regular")

    def test_the_policy_can_be_widened_but_not_invented(self):
        config = load_config(env={"TRADING_MODE": "PAPER",
                                  "MARKET_SESSION_SUBMISSION": "EXTENDED"})
        self.assertEqual(config.trading.session_submission, "extended")
        self.assertEqual(config.trading.describe()["session_submission"], "extended")
        with self.assertRaises(ValueError):
            load_config(env={"TRADING_MODE": "PAPER",
                             "MARKET_SESSION_SUBMISSION": "always"})

    def test_the_status_reports_the_policy_and_its_effect(self):
        from backend.config.settings import TradingSettings
        with self.assertRaises(ValueError):
            TradingSettings(session_submission="24x7")
        self.assertEqual(SESSION_POLICIES, ("regular", "extended"))


class WebhookSessionContractTests(unittest.TestCase):
    """The external contract: what a TradingView alert is told, and when."""

    def _runtime(self, at, **env):
        from backend.tests.test_tradingview_webhook import build_runtime
        return build_runtime(clock=lambda: at, env=env or None)

    def _payload(self, at, **overrides):
        from backend.tests.test_tradingview_webhook import payload
        body = dict(timestamp=at.isoformat().replace("+00:00", "Z"))
        body.update(overrides)
        return payload(**body)

    def test_a_closed_session_is_answered_422_and_never_remembered(self):
        from backend.tests.test_tradingview_webhook import order_rows, send
        saturday = utc(2026, 10, 10, 15, 0)
        runtime = self._runtime(saturday)
        try:
            response = send(runtime, self._payload(saturday))
            self.assertEqual(response.status, 422, response.body)
            self.assertEqual(response.body["code"], "SESSION_CLOSED")
            self.assertEqual(response.body["status"], "rejected")
            self.assertIn("closed", response.body["message"].lower())
            self.assertEqual(len(order_rows(runtime)), 0,
                             "a closed-session alert must create no order")
            self.assertEqual(len(runtime.portfolio.positions), 0)
        finally:
            runtime.close()

    def test_the_same_alert_is_accepted_in_the_next_regular_session(self):
        from backend.tests.test_tradingview_webhook import order_rows, send
        saturday = utc(2026, 10, 10, 15, 0)
        runtime = self._runtime(saturday)
        try:
            self.assertEqual(send(runtime, self._payload(saturday)).status, 422)
            self.assertEqual(len(order_rows(runtime)), 0,
                             "a refusal must not be remembered as a duplicate")
        finally:
            runtime.close()

        monday = utc(2026, 10, 12, 14, 0)
        runtime = self._runtime(monday)
        try:
            response = send(runtime, self._payload(monday))
            self.assertEqual(response.status, 202, response.body)
            self.assertEqual(len(order_rows(runtime)), 1)
        finally:
            runtime.close()

    def test_the_status_exposes_the_policy_and_its_effect(self):
        closed = utc(2026, 10, 10, 15, 0)  # Saturday
        runtime = self._runtime(closed)
        try:
            status = runtime.status()
            self.assertEqual(status["market_session"], "CLOSED")
            self.assertFalse(status["market_trading_day"])
            self.assertEqual(status["market_session_policy"], "regular")
            self.assertFalse(status["market_session_orders_allowed"])
        finally:
            runtime.close()

        open_at = utc(2026, 10, 8, 15, 0)  # Thursday 11:00 ET
        runtime = self._runtime(open_at)
        try:
            status = runtime.status()
            self.assertEqual(status["market_session"], "REGULAR")
            self.assertTrue(status["market_session_orders_allowed"])
        finally:
            runtime.close()

    def test_the_extended_policy_reaches_the_runtime(self):
        pre_market = utc(2026, 10, 8, 12, 0)  # Thursday 08:00 ET
        runtime = self._runtime(pre_market, MARKET_SESSION_SUBMISSION="extended")
        try:
            status = runtime.status()
            self.assertEqual(status["market_session"], "PRE_MARKET")
            self.assertEqual(status["market_session_policy"], "extended")
            self.assertTrue(status["market_session_orders_allowed"],
                            "the paper provider supports extended hours")
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
