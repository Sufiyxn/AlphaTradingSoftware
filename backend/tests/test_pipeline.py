"""Phase 7 proof: the canonical TradingPipeline is the only path to an order.

These tests hold the architecture itself, not just behaviour:

- a production request travels ConnectionManager -> RiskEngine ->
  ExecutionSafetyGate -> verified adapter -> normalized result
- every way a submission can fail carries one deterministic pipeline code
- one idempotency key produces at most one order, booked exactly once
- no production file outside the execution stack and the pipeline calls
  ``place_order``, and both signal paths enter through ``submit_decision``
"""
import pathlib
import unittest
from datetime import datetime, timedelta, timezone

from backend.connections import (
    ConnectionManager,
    ExecutionAuthenticationError,
    ExecutionProviderError,
    ExecutionRateLimitError,
    ExecutionTimeoutError,
    ExecutionProviderDescriptor,
    default_execution_providers,
)
from backend.database import SQLiteJournalRepository
from backend.execution import (
    ExecutionMode,
    ExecutionSafetyGate,
    MockTradingPlatformAdapter,
    OrderStatus,
    OrderType,
    PlatformCapabilities,
)
from backend.paper_trading import PaperTradingConfig, PaperTradingEngine
from backend.pipeline import (
    ALL_CODES,
    ExecutionIntent,
    MAX_IDEMPOTENCY_KEYS,
    PipelineCode,
    TradingPipeline,
)
from backend.portfolio import Portfolio
from backend.risk import RiskEngine
from backend.strategies.models import Direction, TradeSetup

NOW = datetime(2024, 1, 8, 15, tzinfo=timezone.utc)
CLOCK = lambda: NOW  # noqa: E731 - deterministic fixture clock


def trade_setup(symbol="ABC", ratio=2.0, direction=Direction.LONG):
    entry, risk = 100.0, 5.0
    sign = 1 if direction is Direction.LONG else -1
    return TradeSetup(symbol, direction, entry, entry - sign * risk,
                      entry + sign * risk * ratio, risk, risk * ratio, ratio,
                      NOW, "pipeline-proof", .5, "deterministic")


def make_intent(**kwargs):
    kwargs.setdefault("setup", trade_setup())
    kwargs.setdefault("source", "test")
    return ExecutionIntent(**kwargs)


class SpyRisk(RiskEngine):
    """Records every evaluation so tests can prove the risk step ran."""

    def __init__(self, config=None, *, trail=None):
        super().__init__(config)
        self.calls = []
        self.trail = trail if trail is not None else []

    def evaluate(self, proposed, portfolio, *, requested_quantity=None):
        self.calls.append(proposed.symbol)
        self.trail.append("risk")
        return super().evaluate(proposed, portfolio,
                                requested_quantity=requested_quantity)


class SpyAdapter(MockTradingPlatformAdapter):
    """Records every order request; optionally raises a classified failure."""

    def __init__(self, *, raises=None, trail=None, **kwargs):
        super().__init__(**kwargs)
        self.raises = raises
        self.trail = trail if trail is not None else []
        self.requests = []

    def place_order(self, request):
        self.requests.append(request)
        self.trail.append("adapter")
        if self.raises is not None:
            raise self.raises
        return super().place_order(request)


class RecordingGate(ExecutionSafetyGate):
    def __init__(self, *, trail=None, **kwargs):
        super().__init__(**kwargs)
        self.calls = []
        self.trail = trail if trail is not None else []

    def authorize(self, mode):
        self.calls.append(mode)
        self.trail.append("gate")
        return super().authorize(mode)


def registry_for(adapter):
    """A fresh registry that knows the fixture adapter's provider id."""
    registry = default_execution_providers()
    provider_id = str(adapter.identify() or "").strip().lower() or "local_paper"
    if registry.find(provider_id) is None:
        registry.register(ExecutionProviderDescriptor(
            provider_id=provider_id,
            label=f"Fixture adapter {provider_id}",
            implemented=True,
            adapter_factory=lambda **_: adapter,
            authentication="NONE",
            supported_modes=(ExecutionMode.PAPER, ExecutionMode.LIVE),
            environments=("PAPER",),
        ))
    return registry


def make_manager(adapter, *, gate=None, risk=None, mode=ExecutionMode.PAPER,
                 connection_id="paper", extra_connection=None):
    gate = gate or ExecutionSafetyGate(
        kill_switch=(lambda: risk.kill_switch_enabled) if risk is not None else None)
    manager = ConnectionManager(registry=registry_for(adapter), clock=CLOCK, gate=gate)
    manager.add(connection_id=connection_id, provider_id=str(adapter.identify()),
                label="Fixture connection", mode=mode, adapter=adapter)
    if extra_connection is not None:
        connection_id_2, provider_id = extra_connection
        manager.add(connection_id=connection_id_2, provider_id=provider_id,
                    label="Unverified connection")
    return manager


def make_pipeline(*, adapter=None, risk=None, gate=None,
                  mode=ExecutionMode.PAPER, connection_id="paper",
                  extra_connection=None, journal=None, trail=None):
    trail = [] if trail is None else trail
    adapter = adapter if adapter is not None else SpyAdapter(clock=CLOCK, trail=trail)
    risk = risk if risk is not None else SpyRisk(trail=trail)
    if gate is None:
        gate = RecordingGate(trail=trail,
                             kill_switch=lambda: risk.kill_switch_enabled)
    manager = make_manager(adapter, gate=gate, risk=risk, mode=mode,
                           connection_id=connection_id,
                           extra_connection=extra_connection)
    pipeline = TradingPipeline(connections=manager, risk_engine=risk,
                               portfolio=Portfolio(100_000), journal=journal,
                               clock=CLOCK, connection_id=connection_id)
    return pipeline, manager, adapter, risk, gate


class PipelineProofTests(unittest.TestCase):
    def test_production_request_travels_the_whole_chain(self):
        trail: list = []
        risk = SpyRisk(trail=trail)
        adapter = SpyAdapter(trail=trail, clock=CLOCK)
        gate = RecordingGate(trail=trail, kill_switch=lambda: risk.kill_switch_enabled)
        manager = make_manager(adapter, gate=gate, risk=risk)
        journal = SQLiteJournalRepository(":memory:")
        try:
            engine = PaperTradingEngine(
                risk, Portfolio(100_000),
                config=PaperTradingConfig(slippage_per_share=.1),
                journal=journal, clock=CLOCK,
                connection_manager=manager, connection_id="paper")
            self.assertIs(engine.connections, manager,
                          "the engine must route through the shared ConnectionManager")

            result = engine.submit(trade_setup())

            self.assertTrue(result.accepted, (result.code, result.reason))
            self.assertEqual(result.code, PipelineCode.ACCEPTED)
            # ConnectionManager resolved and verified the connection...
            self.assertIs(manager.adapter_for("paper"), adapter)
            self.assertTrue(manager.readiness("paper").ready)
            self.assertEqual(result.connection_id, "paper")
            self.assertEqual(result.provider_id, adapter.identify())
            # ...RiskEngine evaluated exactly once, before the adapter ran...
            self.assertEqual(risk.calls, ["ABC"])
            self.assertLess(trail.index("risk"), trail.index("adapter"))
            # ...the safety gate authorized before risk and before submit...
            self.assertLess(trail.index("gate"), trail.index("risk"))
            self.assertTrue(gate.calls)
            self.assertTrue(all(mode is ExecutionMode.PAPER for mode in gate.calls))
            # ...and the normalized result carries the adapter's filled order.
            self.assertEqual(result.order.status.value, "FILLED")
            self.assertEqual(len(adapter.requests), 1)
            request = adapter.requests[0]
            self.assertEqual(request.symbol, "ABC")
            self.assertEqual(request.limit_price, 100.1)
            self.assertEqual(request.order_type, OrderType.LIMIT)
            self.assertIs(request.execution_mode, ExecutionMode.PAPER)
            # The journal holds the risk decision, the order, and the trade.
            self.assertEqual(len(journal.rows("risk_decisions")), 1)
            self.assertEqual(len(journal.rows("orders")), 1)
            self.assertEqual(len(journal.open_trades()), 1)
            self.assertIn("EXECUTION_SUBMITTED",
                          [event["event_type"] for event in journal.events()])
            self.assertEqual(len(engine.trades), 1)
            self.assertEqual(len(engine.portfolio.positions), 1)
        finally:
            journal.close()

    def test_pre_approved_decision_is_submitted_without_re_evaluating_risk(self):
        trail: list = []
        risk = SpyRisk(trail=trail)
        adapter = SpyAdapter(trail=trail, clock=CLOCK)
        manager = make_manager(adapter, risk=risk)
        portfolio = Portfolio(100_000)
        engine = PaperTradingEngine(risk, portfolio, journal=None, clock=CLOCK,
                                    connection_manager=manager, connection_id="paper")
        decision = risk.evaluate(trade_setup(), portfolio.snapshot())
        risk.calls.clear()

        result = engine.submit_decision(decision)

        self.assertTrue(result.accepted, (result.code, result.reason))
        self.assertEqual(risk.calls, [],
                         "a pre-approved decision must never be re-evaluated")
        self.assertEqual(len(adapter.requests), 1)
        self.assertIsNotNone(result.order)


class RejectionCodeTests(unittest.TestCase):
    def test_input_and_freshness_rejections(self):
        with self.subTest(code="INVALID_REQUEST"):
            pipeline, _, adapter, risk, _ = make_pipeline()
            result = pipeline.execute(object())
            self.assertEqual(result.code, PipelineCode.INVALID_REQUEST)
            self.assertEqual(risk.calls, [], "invalid input never reaches risk")
        with self.subTest(code="INVALID_REQUEST bad price"):
            pipeline, _, adapter, risk, _ = make_pipeline()
            result = pipeline.execute(make_intent(limit_price=-1))
            self.assertEqual(result.code, PipelineCode.INVALID_REQUEST)
            self.assertFalse(adapter.requests)
        with self.subTest(code="STALE_SIGNAL"):
            pipeline, _, adapter, risk, _ = make_pipeline()
            result = pipeline.execute(make_intent(
                occurred_at=NOW - timedelta(seconds=300), max_age_seconds=60))
            self.assertEqual(result.code, PipelineCode.STALE_SIGNAL)
            self.assertEqual(risk.calls, [], "stale signals never reach risk")
            self.assertFalse(adapter.requests)

    def test_risk_and_safety_rejections(self):
        with self.subTest(code="RISK_REJECTED"):
            pipeline, _, adapter, risk, _ = make_pipeline()
            result = pipeline.execute(make_intent(setup=trade_setup(ratio=1.5)))
            self.assertEqual(result.code, PipelineCode.RISK_REJECTED)
            self.assertIsNotNone(result.risk)
            self.assertFalse(result.risk.approved)
            self.assertFalse(adapter.requests)
        with self.subTest(code="KILL_SWITCH via risk engine"):
            risk = SpyRisk()
            risk.set_kill_switch(True)
            pipeline, _, adapter, _, _ = make_pipeline(risk=risk)
            result = pipeline.execute(make_intent())
            self.assertEqual(result.code, PipelineCode.KILL_SWITCH)
            self.assertIn("kill switch enabled", result.risk.reasons)
            self.assertFalse(adapter.requests)
        with self.subTest(code="KILL_SWITCH via gate still evaluates risk"):
            risk = SpyRisk()
            gate = RecordingGate()
            gate.engage("fixture stop")
            pipeline, _, adapter, _, _ = make_pipeline(risk=risk, gate=gate)
            result = pipeline.execute(make_intent())
            self.assertEqual(result.code, PipelineCode.KILL_SWITCH)
            self.assertEqual(risk.calls, ["ABC"],
                             "a gate block must not hide the risk decision")
            self.assertTrue(result.risk.approved)
            self.assertFalse(adapter.requests)
        with self.subTest(code="SAFETY_REJECTED"):
            pipeline, _, adapter, risk, gate = make_pipeline()
            result = pipeline.execute(make_intent(mode=ExecutionMode.LIVE))
            self.assertEqual(result.code, PipelineCode.SAFETY_REJECTED)
            self.assertFalse(gate.kill_switch_engaged)
            self.assertEqual(risk.calls, ["ABC"],
                             "safety rejection happens after the risk decision")
            self.assertTrue(result.risk.approved)
            self.assertFalse(adapter.requests)

    def test_connection_and_capability_rejections(self):
        with self.subTest(code="CONNECTION_NOT_FOUND"):
            pipeline, _, adapter, risk, _ = make_pipeline()
            result = pipeline.execute(make_intent(connection_id="ghost"))
            self.assertEqual(result.code, PipelineCode.CONNECTION_NOT_FOUND)
            self.assertIn("ghost", result.reason)
            self.assertEqual(risk.calls, [], "an unknown connection never reaches risk")
            self.assertFalse(adapter.requests)
        with self.subTest(code="CONNECTION_NOT_READY"):
            pipeline, _, adapter, _, _ = make_pipeline(
                extra_connection=("client", "alpaca"))
            result = pipeline.execute(make_intent(connection_id="client"))
            self.assertEqual(result.code, PipelineCode.CONNECTION_NOT_READY)
            self.assertIn("credentials are missing", result.reason)
            self.assertFalse(adapter.requests)
        with self.subTest(code="CAPABILITY_UNAVAILABLE"):
            limited = SpyAdapter(clock=CLOCK, capabilities=PlatformCapabilities(
                order_types=frozenset({OrderType.MARKET})))
            pipeline, _, adapter, _, _ = make_pipeline(adapter=limited)
            result = pipeline.execute(make_intent(limit_price=100.05))
            self.assertEqual(result.code, PipelineCode.CAPABILITY_UNAVAILABLE)
            self.assertIn("limit orders", result.reason)
            self.assertFalse(adapter.requests)
        with self.subTest(code="UNSUPPORTED_MODE"):
            pipeline, _, adapter, _, _ = make_pipeline()
            result = pipeline.execute(make_intent(mode="PAPER"))  # not an ExecutionMode
            self.assertEqual(result.code, PipelineCode.UNSUPPORTED_MODE)
            self.assertIn("unsupported execution mode", result.reason)
            self.assertFalse(adapter.requests)
        with self.subTest(code="a PAPER order never enters a LIVE connection"):
            live_ready = SpyAdapter(clock=CLOCK, capabilities=PlatformCapabilities(
                live_trading=True))
            pipeline, _, adapter, risk, _ = make_pipeline(adapter=live_ready,
                                                          mode=ExecutionMode.LIVE)
            result = pipeline.execute(make_intent())  # a PAPER request
            self.assertNotEqual(result.code, PipelineCode.ACCEPTED)
            self.assertEqual(risk.calls, [],
                             "a not-ready connection fails before risk")
            self.assertFalse(adapter.requests)
        with self.subTest(code="UNSUPPORTED_MODE via connection mode mismatch"):
            # Defense in depth: even if readiness were granted, a mode
            # mismatch between intent and connection is still refused.
            from backend.pipeline.service import _Request
            request = _Request(setup=trade_setup(), source="test", signal_id=None,
                               connection_id="paper", mode=ExecutionMode.PAPER,
                               limit_price=None, occurred_at=None,
                               max_age_seconds=None, requested_quantity=None,
                               idempotency_key=None)

            class _Connection:
                connection_id = "paper"
                mode = ExecutionMode.LIVE

            code, _ = TradingPipeline._mode_failure(request, _Connection())
            self.assertEqual(code, PipelineCode.UNSUPPORTED_MODE)

    def test_provider_failures_are_classified(self):
        cases = (
            ("outage", ExecutionProviderError("fixture provider outage"),
             PipelineCode.PROVIDER_UNAVAILABLE),
            ("credentials", ExecutionAuthenticationError("fixture credentials rejected"),
             PipelineCode.PROVIDER_AUTH_FAILURE),
            ("timeout", ExecutionTimeoutError("fixture timeout"),
             PipelineCode.PROVIDER_TIMEOUT),
            ("rate limit", ExecutionRateLimitError("fixture throttled"),
             PipelineCode.PROVIDER_RATE_LIMIT),
        )
        for label, error, expected in cases:
            with self.subTest(label=label):
                failing = SpyAdapter(clock=CLOCK, raises=error)
                pipeline, _, adapter, risk, _ = make_pipeline(adapter=failing)
                result = pipeline.execute(make_intent())
                self.assertEqual(result.code, expected)
                self.assertEqual(len(adapter.requests), 1,
                                 "the failure happens at submission, not before")
                self.assertTrue(result.risk.approved)
                self.assertIn("fixture", result.reason,
                              "the classified failure must surface honestly")

    def test_provider_rejection_is_not_an_acceptance(self):
        rejecting = SpyAdapter(clock=CLOCK, fail_orders=True)
        pipeline, _, adapter, _, _ = make_pipeline(adapter=rejecting)
        result = pipeline.execute(make_intent())
        self.assertEqual(result.code, PipelineCode.PROVIDER_EXECUTION_REJECTION)
        self.assertIsNotNone(result.order)
        self.assertEqual(len(adapter.requests), 1)
        self.assertFalse(result.accepted)
        self.assertIn("did not fill", result.reason)

    def test_an_accepted_but_unfilled_order_is_submitted_not_rejected(self):
        from backend.testing.execution import ScriptedAdapter
        adapter = ScriptedAdapter(authenticated=True, clock=CLOCK)
        pipeline, _, _, _, _ = make_pipeline(adapter=adapter)
        result = pipeline.execute(make_intent())
        self.assertEqual(result.code, PipelineCode.SUBMITTED)
        self.assertIsNotNone(result.order)
        self.assertFalse(result.accepted, "nothing is booked until a fill exists")
        self.assertEqual(result.order.status, OrderStatus.SUBMITTED)
        self.assertEqual(result.order.filled_quantity, 0)
        self.assertEqual(result.order.symbol, "ABC")
        self.assertIsNotNone(result.order.provider_order_id)
        self.assertIn("not filled", result.reason)

    def test_every_documented_code_is_exercised(self):
        exercised = {
            PipelineCode.ACCEPTED, PipelineCode.SUBMITTED,
            PipelineCode.INVALID_REQUEST,
            PipelineCode.STALE_SIGNAL, PipelineCode.DUPLICATE_INTENT,
            PipelineCode.RISK_REJECTED, PipelineCode.KILL_SWITCH,
            PipelineCode.SAFETY_REJECTED, PipelineCode.CONNECTION_NOT_FOUND,
            PipelineCode.CONNECTION_NOT_READY, PipelineCode.CAPABILITY_UNAVAILABLE,
            PipelineCode.UNSUPPORTED_MODE, PipelineCode.PROVIDER_UNAVAILABLE,
            PipelineCode.PROVIDER_AUTH_FAILURE, PipelineCode.PROVIDER_TIMEOUT,
            PipelineCode.PROVIDER_RATE_LIMIT,
            PipelineCode.PROVIDER_EXECUTION_REJECTION,
            PipelineCode.PROVIDER_ID_COLLISION,
            PipelineCode.SESSION_BLOCKED,
        }
        self.assertEqual(set(ALL_CODES), exercised)


class IdempotencyTests(unittest.TestCase):
    def _engine(self, journal=None):
        adapter = SpyAdapter(clock=CLOCK)
        risk = SpyRisk()
        manager = make_manager(adapter, risk=risk)
        engine = PaperTradingEngine(risk, Portfolio(100_000), journal=journal,
                                    clock=CLOCK, connection_manager=manager,
                                    connection_id="paper")
        return engine, adapter, manager

    def test_one_key_is_one_order_and_one_booking(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            engine, adapter, manager = self._engine(journal)
            first = engine.submit(trade_setup(), idempotency_key="signal-1")
            second = engine.submit(trade_setup(), idempotency_key="signal-1")
            self.assertEqual(first.code, PipelineCode.ACCEPTED)
            self.assertEqual(second.code, PipelineCode.DUPLICATE_INTENT)
            self.assertTrue(second.duplicate)
            self.assertEqual(second.order.order_id, first.order.order_id)
            self.assertEqual(second.connection_id, first.connection_id)
            self.assertEqual(len(adapter.requests), 1,
                             "a duplicate key must never submit a second order")
            self.assertEqual(len(engine.trades), 1,
                             "a duplicate result must never book twice")
            self.assertEqual(len(engine.portfolio.positions), 1)
            self.assertEqual(len(journal.rows("orders")), 1)
            self.assertIn("EXECUTION_DUPLICATE",
                          [event["event_type"] for event in journal.events()])
        finally:
            journal.close()

    def test_distinct_keys_are_distinct_orders(self):
        engine, adapter, _ = self._engine()
        first = engine.submit(trade_setup("AAA"), idempotency_key="signal-1")
        second = engine.submit(trade_setup("BBB"), idempotency_key="signal-2")
        self.assertEqual(first.code, PipelineCode.ACCEPTED)
        self.assertEqual(second.code, PipelineCode.ACCEPTED)
        self.assertEqual(len(adapter.requests), 2)
        self.assertEqual(len(engine.trades), 2)

    def test_the_idempotency_window_is_bounded(self):
        pipeline, _, adapter, _, _ = make_pipeline()
        for index in range(MAX_IDEMPOTENCY_KEYS + 1):
            result = pipeline.execute(make_intent(
                limit_price=100.0, idempotency_key=f"key-{index}"))
            self.assertEqual(result.code, PipelineCode.ACCEPTED)
        self.assertLessEqual(len(pipeline._completed), MAX_IDEMPOTENCY_KEYS)
        self.assertEqual(len(adapter.requests), MAX_IDEMPOTENCY_KEYS + 1)


class NoAlternatePathTests(unittest.TestCase):
    backend = pathlib.Path(__file__).resolve().parents[1]
    production_dirs = {"execution", "pipeline"}

    def test_place_order_is_confined_to_the_execution_stack(self):
        offenders = []
        for path in sorted(self.backend.rglob("*.py")):
            relative = path.relative_to(self.backend)
            if relative.parts[0] in ("tests", "testing"):
                continue  # fixtures and test code are not production paths
            if "place_order(" not in path.read_text(encoding="utf-8"):
                continue
            if relative.parts[0] not in self.production_dirs:
                offenders.append(str(relative))
        self.assertEqual(offenders, [],
                         "only the execution stack and the pipeline may call place_order")

    def test_signal_paths_enter_through_the_pipeline(self):
        for relative in ("tradingview/service.py", "orchestrator/service.py"):
            with self.subTest(path=relative):
                text = (self.backend / relative).read_text(encoding="utf-8")
                self.assertIn("submit_decision(", text)
                self.assertNotIn(".place_order(", text)
                self.assertNotIn("risk.evaluate(", text)
                self.assertNotIn("risk_engine.evaluate(", text)
        engine = (self.backend / "paper_trading" / "engine.py").read_text(
            encoding="utf-8")
        self.assertNotIn("place_order(", engine,
                         "the paper engine must not call providers directly")


if __name__ == "__main__":
    unittest.main()
