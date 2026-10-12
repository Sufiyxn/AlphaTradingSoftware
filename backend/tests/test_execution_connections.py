"""Phase 6 - Connection & Execution Provider Framework tests.

Covers the execution provider registry and interface, the connection manager
lifecycle and honest states, credential redaction, capability discovery, the
normalized order/status models, the paper/live boundary, multiple client
connections, Connection Center + HTTP reporting, frontend/vendor separation,
and the Phase 1-5 regression surface (market data, RiskEngine).
"""
import json
import re
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError

from backend.config import BROKER_CATALOG, load_config
from backend.connections import (
    KNOWN_CAPABILITIES,
    AccountVerificationState,
    AuthenticationState,
    ConnectionManager,
    ConnectionState,
    CredentialHandle,
    ExecutionNotInstalledError,
    ExecutionProviderDescriptor,
    ExecutionProviderRegistry,
    PermissionState,
    UnknownExecutionProviderError,
    default_execution_providers,
)
from backend.core.runtime import ApplicationRuntime
from backend.core.server import _json_default, create_server
from backend.execution import (
    AssetType,
    ExecutionMode,
    ExecutionRouter,
    ExecutionSafetyGate,
    LocalPaperAdapter,
    MockTradingPlatformAdapter,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    PlatformCapabilities,
    TradingPlatformAdapter,
    normalize_order_status,
)
from backend.strategies.models import Direction
from backend.testing import (
    ScriptedAdapter,
    account_failure,
    auth_failure,
    connection_failure,
    full_paper_capabilities,
    rate_limit_failure,
    timeout_failure,
)

CLOCK = datetime(2024, 1, 2, 15, 29, 0, 1, tzinfo=timezone.utc)
SECRET = "execution-secret-never-serialize-this"
OWNER_TOKEN = "owner-test-token"
ROOT = Path(__file__).resolve().parents[2]


class Clock:
    """Mutable single clock so staleness can be exercised without sleeping."""

    def __init__(self, start: datetime = CLOCK) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def scripted_registry(**adapter_kwargs) -> ExecutionProviderRegistry:
    """Default registry plus one implemented provider whose adapter is scripted."""
    registry = default_execution_providers()
    registry.register(ExecutionProviderDescriptor(
        provider_id="scripted", label="Scripted provider", implemented=True,
        adapter_factory=lambda **_: ScriptedAdapter(**adapter_kwargs),
        authentication="API_KEY_SECRET",
        supported_modes=(ExecutionMode.PAPER, ExecutionMode.LIVE),
        asset_classes=("equities",),
        declared_capabilities=(),
        credential_fields=("SCRIPTED_API_KEY",),
        environments=("SANDBOX", "LIVE"),
        metadata=(("status", "test-only scripted provider"),),
    ))
    return registry


def credentials() -> CredentialHandle:
    return CredentialHandle({"SCRIPTED_API_KEY": SECRET})


def build_manager(registry=None, clock=None, gate=None, stale_after_seconds=300.0):
    return ConnectionManager(
        registry=registry or default_execution_providers(),
        clock=clock or Clock(), gate=gate, stale_after_seconds=stale_after_seconds)


def build_runtime(*, env=None, owner_token=OWNER_TOKEN, clock=None):
    defaults = {"OWNER_CONTROL_TOKEN": owner_token}
    defaults.update(env or {})
    config = load_config(env=defaults)
    return ApplicationRuntime(database_path=":memory:", owner_token=owner_token,
                              clock=clock or (lambda: CLOCK), config=config)


class ExecutionProviderRegistryTests(unittest.TestCase):
    def test_default_registry_ships_paper_and_advertises_external_providers(self):
        registry = default_execution_providers()
        self.assertEqual(registry.ids(), ("alpaca", "local_paper", "robinhood"))
        self.assertTrue(registry.is_implemented("local_paper"))
        self.assertFalse(registry.is_implemented("alpaca"))
        self.assertFalse(registry.is_implemented("robinhood"))
        alpaca = registry.get("alpaca")
        self.assertEqual(alpaca.authentication, "API_KEY_SECRET")
        self.assertIn(ExecutionMode.LIVE, alpaca.supported_modes)
        robinhood = registry.get("robinhood")
        self.assertEqual(robinhood.supported_modes, ())
        self.assertIn("no adapter", robinhood.metadata[0][1])

    def test_registry_rejects_inconsistent_or_invalid_descriptors(self):
        registry = ExecutionProviderRegistry()
        with self.assertRaises(ValueError):
            ExecutionProviderDescriptor(provider_id="broken", label="Broken",
                                        implemented=True, adapter_factory=None)
        with self.assertRaises(ValueError):
            ExecutionProviderDescriptor(provider_id="broken", label="Broken",
                                        implemented=False, adapter_factory=lambda **_: None)
        with self.assertRaises(ValueError):
            ExecutionProviderDescriptor(provider_id="bad id", label="Bad",
                                        implemented=False, authentication="NONE")
        with self.assertRaises(ValueError):
            ExecutionProviderDescriptor(provider_id="odd", label="Odd", implemented=False,
                                        authentication="SOMETHING_ELSE")
        registry.register(ExecutionProviderDescriptor(
            provider_id="first", label="First", implemented=False))
        with self.assertRaises(ValueError):
            registry.register(ExecutionProviderDescriptor(
                provider_id="first", label="First again", implemented=False))
        with self.assertRaises(TypeError):
            registry.register("not-a-descriptor")

    def test_unknown_and_unimplemented_providers_fail_honestly(self):
        registry = default_execution_providers()
        with self.assertRaises(UnknownExecutionProviderError):
            registry.build("mystery")
        with self.assertRaises(ExecutionNotInstalledError) as caught:
            registry.build("alpaca")
        self.assertIn("not installed", str(caught.exception))
        self.assertIn("no network request was made", str(caught.exception))

    def test_descriptor_description_hides_the_factory_and_carries_no_secret(self):
        registry = scripted_registry()
        described = json.dumps(registry.describe(), default=_json_default)
        self.assertNotIn("adapter_factory", described)
        self.assertNotIn("<lambda>", described)
        self.assertNotIn(SECRET, described)
        local = registry.get("local_paper").describe()
        self.assertTrue(local["implemented"])
        self.assertEqual(local["credential_fields"], [])

    def test_execution_registry_stays_consistent_with_the_config_catalog(self):
        registry = default_execution_providers()
        for provider_id, entry in BROKER_CATALOG.items():
            with self.subTest(provider=provider_id):
                descriptor = registry.find(provider_id)
                self.assertIsNotNone(descriptor)
                self.assertEqual(registry.is_implemented(provider_id), entry.implemented)
        for descriptor in registry.descriptors():
            with self.subTest(provider=descriptor.provider_id, implemented=True):
                if descriptor.implemented:
                    self.assertTrue(callable(descriptor.adapter_factory))
                else:
                    with self.assertRaises(ExecutionNotInstalledError):
                        descriptor.build()

    def test_execution_interface_contract_is_complete(self):
        expected = {
            "identify", "capabilities", "is_authenticated", "connect", "disconnect",
            "health_check", "get_account", "get_positions", "get_orders", "get_order",
            "reconcile_orders", "place_order", "cancel_order", "replace_order", "get_quote",
        }
        members = {name for name in TradingPlatformAdapter.__dict__
                   if not name.startswith("_")}
        self.assertLessEqual(expected, members)


class ConnectionLifecycleTests(unittest.TestCase):
    def test_local_paper_connection_verifies_in_process(self):
        clock = Clock()
        manager = build_manager(clock=clock)
        record = manager.add(
            connection_id="paper", provider_id="local_paper",
            adapter=LocalPaperAdapter(cash=1_000.0, clock=clock))
        self.assertEqual(record.current_state()[0], ConnectionState.CONNECTED)
        readiness = manager.readiness("paper")
        self.assertTrue(readiness.ready)
        self.assertTrue(readiness.configured)
        self.assertTrue(readiness.authenticated)
        self.assertTrue(readiness.connected)
        self.assertTrue(readiness.account_verified)
        self.assertTrue(readiness.trading_enabled)
        self.assertEqual(readiness.mode, ExecutionMode.PAPER)
        self.assertIsNone(readiness.failure_reason)
        self.assertIn("no external provider was contacted", record.describe()["reason"])
        self.assertIn("paper_trading", record.capabilities)
        self.assertNotIn("live_trading", record.capabilities)

    def test_configuration_without_verification_is_never_connected(self):
        manager = build_manager(registry=scripted_registry())
        record = manager.add(connection_id="alpha", provider_id="scripted",
                             credentials=credentials(), verify=False)
        self.assertTrue(record.configured)
        self.assertEqual(record.current_state()[0], ConnectionState.CONFIGURED)
        readiness = manager.readiness("alpha")
        self.assertFalse(readiness.ready)
        self.assertTrue(readiness.configured)
        self.assertFalse(readiness.connected)
        self.assertEqual(readiness.state, "CONFIGURED")
        self.assertIn("authentication has not been confirmed", readiness.failure_reason)
        result = manager.verify("alpha")
        self.assertTrue(result.ready)
        self.assertEqual(result.state, "CONNECTED")
        self.assertEqual(record.authentication, AuthenticationState.AUTHENTICATED)

    def test_credentials_alone_never_produce_ready(self):
        manager = build_manager(registry=scripted_registry())
        record = manager.add(connection_id="alpha", provider_id="scripted",
                             credentials=credentials(), verify=False)
        described = record.describe()
        self.assertTrue(described["configured"])
        self.assertFalse(described["ready"])
        self.assertNotEqual(described["state"], "CONNECTED")
        self.assertFalse(manager.describe()["ready"])

    def test_missing_credentials_report_not_configured_and_build_nothing(self):
        manager = build_manager(registry=scripted_registry())
        record = manager.add(connection_id="alpha", provider_id="scripted", verify=True)
        self.assertFalse(record.configured)
        self.assertEqual(record.current_state()[0], ConnectionState.NOT_CONFIGURED)
        self.assertIn("SCRIPTED_API_KEY", record.describe()["reason"])
        self.assertIn("credentials are missing", record.describe()["reason"])
        self.assertIsNone(manager.adapter_for("alpha"))
        self.assertFalse(manager.readiness("alpha").ready)

    def test_unknown_provider_reports_error(self):
        manager = build_manager()
        record = manager.add(connection_id="mystery", provider_id="mystery",
                             credentials=CredentialHandle({"X": "y"}), verify=True)
        self.assertFalse(record.known)
        self.assertEqual(record.current_state()[0], ConnectionState.ERROR)
        self.assertIn("not a known execution provider", record.describe()["reason"])
        self.assertFalse(manager.readiness("mystery").ready)

    def test_unimplemented_provider_is_unavailable_without_a_network_request(self):
        manager = build_manager()
        record = manager.add(
            connection_id="client", provider_id="alpaca",
            credentials=CredentialHandle({"ALPACA_API_KEY": "key-value",
                                          "ALPACA_API_SECRET": "secret-value"}),
            verify=True)
        self.assertTrue(record.configured)
        self.assertFalse(record.implemented)
        state, reason = record.current_state()
        self.assertEqual(state, ConnectionState.UNAVAILABLE)
        self.assertIn("not installed", reason)
        self.assertIn("no network request was made", reason)
        self.assertIsNone(manager.adapter_for("client"))
        readiness = manager.readiness("client")
        self.assertFalse(readiness.ready)
        self.assertFalse(readiness.authenticated)

    def test_authentication_failure_is_reported_and_redacted(self):
        manager = build_manager(registry=scripted_registry(
            connect_error=auth_failure(f"the provider rejected {SECRET}")))
        record = manager.add(connection_id="alpha", provider_id="scripted",
                             credentials=credentials(), verify=True)
        self.assertEqual(record.current_state()[0], ConnectionState.ERROR)
        self.assertEqual(record.authentication, AuthenticationState.FAILED)
        self.assertIn("authentication failed", record.describe()["reason"])
        self.assertNotIn(SECRET, json.dumps(record.describe(), default=_json_default))
        self.assertFalse(manager.readiness("alpha").ready)

    def test_connectivity_failure_reports_unavailable(self):
        manager = build_manager(registry=scripted_registry(
            connect_error=connection_failure("the provider host did not answer")))
        record = manager.add(connection_id="alpha", provider_id="scripted",
                             credentials=credentials(), verify=True)
        self.assertEqual(record.current_state()[0], ConnectionState.UNAVAILABLE)
        self.assertEqual(record.authentication, AuthenticationState.UNVERIFIED)
        self.assertIn("did not answer", record.describe()["reason"])

    def test_timeout_reports_unavailable_with_the_timeout_reason(self):
        manager = build_manager(registry=scripted_registry(
            connect_error=timeout_failure("timed out after 10 seconds")))
        record = manager.add(connection_id="alpha", provider_id="scripted",
                             credentials=credentials(), verify=True)
        self.assertEqual(record.current_state()[0], ConnectionState.UNAVAILABLE)
        self.assertIn("timed out after 10 seconds", record.describe()["reason"])
        self.assertFalse(manager.readiness("alpha").ready)

    def test_rate_limit_reports_degraded(self):
        manager = build_manager(registry=scripted_registry(
            connect_error=rate_limit_failure("rate limited: 60 calls per minute")))
        record = manager.add(connection_id="alpha", provider_id="scripted",
                             credentials=credentials(), verify=True)
        self.assertEqual(record.current_state()[0], ConnectionState.DEGRADED)
        self.assertIn("rate limited", record.describe()["reason"])
        self.assertEqual(record.authentication, AuthenticationState.UNVERIFIED)

    def test_account_verification_failure_reports_error(self):
        manager = build_manager(registry=scripted_registry(
            account_error=account_failure("the account lookup was rejected")))
        record = manager.add(connection_id="alpha", provider_id="scripted",
                             credentials=credentials(), verify=True)
        self.assertEqual(record.current_state()[0], ConnectionState.ERROR)
        self.assertEqual(record.account_state, AccountVerificationState.FAILED)
        self.assertIn("account verification failed", record.describe()["reason"])
        self.assertFalse(manager.readiness("alpha").account_verified)

    def test_unsupported_account_verification_is_not_claimed(self):
        caps = PlatformCapabilities(
            asset_types=frozenset({AssetType.STOCK}), order_types=frozenset(OrderType),
            account_retrieval=False, position_retrieval=False, order_retrieval=False)
        manager = build_manager(registry=scripted_registry(capabilities=caps))
        record = manager.add(connection_id="alpha", provider_id="scripted",
                             credentials=credentials(), verify=True)
        self.assertEqual(record.account_state, AccountVerificationState.UNSUPPORTED)
        readiness = manager.readiness("alpha")
        self.assertTrue(readiness.connected)
        self.assertFalse(readiness.account_verified)
        self.assertFalse(readiness.ready)
        self.assertIn("does not expose account verification", readiness.failure_reason)

    def test_capability_discovery_reports_only_supported_capabilities(self):
        caps = PlatformCapabilities(
            asset_types=frozenset({AssetType.STOCK}),
            order_types=frozenset({OrderType.MARKET, OrderType.LIMIT}),
            short_selling=False, fractional_shares=False, extended_hours=False,
            order_replacement=False, order_cancellation=True,
            position_retrieval=True, account_retrieval=True, order_retrieval=True,
            paper_trading=True, live_trading=False)
        manager = build_manager(registry=scripted_registry(capabilities=caps))
        record = manager.add(connection_id="alpha", provider_id="scripted",
                             credentials=credentials(), verify=True)
        self.assertIn("limit_orders", record.capabilities)
        self.assertNotIn("stop_orders", record.capabilities)
        self.assertNotIn("short_selling", record.capabilities)
        self.assertNotIn("live_trading", record.capabilities)
        self.assertIn("stop_orders", record.capability_limitations)
        self.assertIn("short_selling", record.capability_limitations)
        self.assertIn("live_trading", record.capability_limitations)
        for name in record.capabilities:
            self.assertIn(name, KNOWN_CAPABILITIES)
        readiness = manager.readiness("alpha")
        self.assertTrue(readiness.capability_limitations)

    def test_degraded_health_probe_is_reported_honestly(self):
        manager = build_manager(registry=scripted_registry(
            health={"success": False, "status": "DEGRADED",
                    "detail": "the provider reported elevated errors"}))
        record = manager.add(connection_id="alpha", provider_id="scripted",
                             credentials=credentials(), verify=True)
        self.assertEqual(record.current_state()[0], ConnectionState.DEGRADED)
        self.assertIn("elevated errors", record.describe()["reason"])
        self.assertFalse(manager.readiness("alpha").ready)

    def test_successful_verification_goes_stale_after_its_window(self):
        clock = Clock()
        manager = build_manager(clock=clock, stale_after_seconds=60)
        record = manager.add(connection_id="paper", provider_id="local_paper",
                             adapter=LocalPaperAdapter(cash=1_000.0, clock=clock))
        self.assertEqual(record.current_state()[0], ConnectionState.CONNECTED)
        clock.advance(61)
        state, reason = record.current_state()
        self.assertEqual(state, ConnectionState.STALE)
        self.assertIn("older than 60 seconds", reason)
        readiness = manager.readiness("paper")
        self.assertFalse(readiness.ready)
        self.assertFalse(readiness.connected)
        self.assertIn("older than 60 seconds", readiness.failure_reason)

    def test_connecting_state_is_visible_while_verification_runs(self):
        clock = Clock()
        manager = build_manager(clock=clock)
        manager.add(connection_id="paper", provider_id="local_paper",
                    adapter=LocalPaperAdapter(cash=1_000.0, clock=clock), verify=False)
        record = manager.get("paper")
        record.begin(clock())
        self.assertEqual(record.current_state()[0], ConnectionState.CONNECTING)
        self.assertIn("in progress", record.describe()["reason"])


class PaperLiveSeparationTests(unittest.TestCase):
    def test_gate_allows_paper_and_blocks_live(self):
        gate = ExecutionSafetyGate()
        allowed, reason = gate.authorize(ExecutionMode.PAPER)
        self.assertTrue(allowed)
        allowed, reason = gate.authorize(ExecutionMode.LIVE)
        self.assertFalse(allowed)
        self.assertIn("no live execution adapter", reason)
        self.assertIn("safety gates", reason)
        described = gate.describe()
        self.assertTrue(described["paper_allowed"])
        self.assertFalse(described["live_allowed"])
        self.assertFalse(described["kill_switch_engaged"])

    def test_gate_reads_the_risk_kill_switch_and_its_own_latch(self):
        engaged = {"value": False}
        gate = ExecutionSafetyGate(kill_switch=lambda: engaged["value"])
        allowed, _ = gate.authorize(ExecutionMode.PAPER)
        self.assertTrue(allowed)
        engaged["value"] = True
        allowed, reason = gate.authorize(ExecutionMode.PAPER)
        self.assertFalse(allowed)
        self.assertIn("risk emergency stop", reason)
        engaged["value"] = False
        gate.engage("owner stopped execution")
        allowed, reason = gate.authorize(ExecutionMode.PAPER)
        self.assertFalse(allowed)
        self.assertIn("owner stopped execution", reason)
        self.assertTrue(gate.describe()["manual_stop_engaged"])
        gate.release("owner resumed")
        allowed, _ = gate.authorize(ExecutionMode.PAPER)
        self.assertTrue(allowed)

    def test_live_connection_is_never_ready_in_this_build(self):
        manager = build_manager(registry=scripted_registry())
        record = manager.add(connection_id="live", provider_id="scripted",
                             mode=ExecutionMode.LIVE, environment="LIVE",
                             credentials=credentials(), verify=True)
        readiness = manager.readiness("live")
        self.assertEqual(record.mode, ExecutionMode.LIVE)
        self.assertEqual(record.permission, PermissionState.DENIED)
        self.assertFalse(readiness.trading_enabled)
        self.assertFalse(readiness.ready)
        self.assertIn("does not support live trading", readiness.failure_reason)
        # Even a provider that did support live trading stays blocked by the gate.
        live_caps = PlatformCapabilities(
            asset_types=frozenset({AssetType.STOCK}), order_types=frozenset(OrderType),
            short_selling=True, fractional_shares=True, extended_hours=True,
            order_replacement=True, paper_trading=True, live_trading=True)
        gated = build_manager(registry=scripted_registry(capabilities=live_caps))
        gated_record = gated.add(connection_id="live", provider_id="scripted",
                                 mode=ExecutionMode.LIVE, environment="LIVE",
                                 credentials=credentials(), verify=True)
        gated_result = gated.readiness("live")
        self.assertEqual(gated_record.permission, PermissionState.GRANTED)
        self.assertEqual(gated_record.current_state()[0], ConnectionState.CONNECTED)
        self.assertFalse(gated_result.ready)
        self.assertIn("not implemented yet", gated_result.failure_reason)

    def test_paper_execution_stays_available_while_live_is_locked(self):
        runtime = build_runtime()
        try:
            readiness = runtime.connections.readiness()
            self.assertEqual(readiness["live_mode"], "LOCKED")
            self.assertEqual(readiness["execution_state"], "CONNECTED")
            self.assertTrue(any(item["key"] == "paper_mode" and item["passed"]
                                for item in readiness["checks"]))
        finally:
            runtime.close()


class CredentialHandlingTests(unittest.TestCase):
    def test_handle_exposes_presence_only(self):
        handle = CredentialHandle({"SCRIPTED_API_KEY": SECRET})
        self.assertEqual(handle.present(), ("SCRIPTED_API_KEY",))
        self.assertEqual(handle.missing(("SCRIPTED_API_KEY", "OTHER")), ("OTHER",))
        self.assertTrue(handle.has("scripted_api_key"))
        self.assertEqual(handle.get("SCRIPTED_API_KEY"), SECRET)
        self.assertEqual(handle.describe(("SCRIPTED_API_KEY", "OTHER")),
                         [{"name": "SCRIPTED_API_KEY", "configured": True},
                          {"name": "OTHER", "configured": False}])
        for text in (repr(handle), str(handle)):
            self.assertNotIn(SECRET, text)
        self.assertIn("fields=1", repr(handle))

    def test_handle_redacts_values_in_error_text(self):
        handle = CredentialHandle({"SCRIPTED_API_KEY": SECRET})
        self.assertEqual(handle.redact(f"rejected key {SECRET} end"), "rejected key *** end")
        self.assertEqual(handle.redact(None), "")

    def test_connection_metadata_never_contains_a_credential(self):
        manager = build_manager(registry=scripted_registry(
            connect_error=auth_failure(f"rejected {SECRET}")))
        record = manager.add(connection_id="alpha", provider_id="scripted",
                             credentials=CredentialHandle({"SCRIPTED_API_KEY": SECRET}),
                             verify=True)
        document = json.dumps(
            {"record": record.describe(), "manager": manager.describe()},
            default=_json_default)
        self.assertNotIn(SECRET, document)
        self.assertEqual(record.describe()["credential_fields"],
                         [{"name": "SCRIPTED_API_KEY", "configured": True}])


class MultipleConnectionTests(unittest.TestCase):
    def test_three_client_connections_are_tracked_independently(self):
        clock = Clock()
        manager = build_manager(registry=scripted_registry(), clock=clock)
        manager.add(connection_id="paper", provider_id="local_paper",
                    adapter=LocalPaperAdapter(cash=1_000.0, clock=clock))
        manager.add(connection_id="alpha", provider_id="scripted",
                    label="Connection A", credentials=credentials(), verify=True)
        manager.add(connection_id="broken", provider_id="scripted",
                    label="Connection B", verify=True)
        self.assertEqual(manager.connection_ids(), ("paper", "alpha", "broken"))
        self.assertTrue(manager.readiness("paper").ready)
        self.assertTrue(manager.readiness("alpha").ready)
        self.assertFalse(manager.readiness("broken").ready)
        described = manager.describe()
        self.assertEqual(described["count"], 3)
        self.assertEqual(described["ready_count"], 2)
        states = {item["connection_id"]: item["state"]
                  for item in described["connections"]}
        self.assertEqual(states["paper"], "CONNECTED")
        self.assertEqual(states["broken"], "NOT_CONFIGURED")
        self.assertNotEqual(states["alpha"], states["broken"])

    def test_connection_ids_are_validated_and_unique(self):
        manager = build_manager()
        with self.assertRaises(ValueError):
            manager.add(connection_id="bad id!", provider_id="local_paper")
        with self.assertRaises(ValueError):
            manager.add(connection_id="", provider_id="local_paper")
        manager.add(connection_id="paper", provider_id="local_paper",
                    adapter=LocalPaperAdapter())
        with self.assertRaises(ValueError):
            manager.add(connection_id="paper", provider_id="local_paper")
        with self.assertRaises(KeyError):
            manager.get("missing")

    def test_aggregate_state_surfaces_configured_and_failing_connections(self):
        clock = Clock()
        manager = build_manager(clock=clock)
        manager.add(connection_id="paper", provider_id="local_paper",
                    adapter=LocalPaperAdapter(cash=1.0, clock=clock))
        state, _ = manager.aggregate_state()
        self.assertEqual(state, "CONNECTED")
        manager.add(connection_id="client", provider_id="alpaca",
                    credentials=CredentialHandle({"ALPACA_API_KEY": "k",
                                                  "ALPACA_API_SECRET": "s"}))
        state, _ = manager.aggregate_state()
        self.assertEqual(state, "CONFIGURED")
        manager.verify("client")
        state, reason = manager.aggregate_state()
        self.assertEqual(state, "UNAVAILABLE")
        self.assertIn("not installed", reason)


class OrderNormalizationTests(unittest.TestCase):
    def test_normalized_status_vocabulary_is_complete(self):
        values = {status.value for status in OrderStatus}
        for required in ("SUBMITTED", "ACCEPTED", "REJECTED", "PARTIALLY_FILLED",
                         "FILLED", "CANCELLED", "EXPIRED", "FAILED", "PENDING",
                         "UNKNOWN"):
            with self.subTest(status=required):
                self.assertIn(required, values)

    def test_provider_status_words_are_normalized(self):
        table = {
            "new": OrderStatus.ACCEPTED,
            "submitted": OrderStatus.SUBMITTED,
            "PARTIALLY FILLED": OrderStatus.PARTIALLY_FILLED,
            "partially-filled": OrderStatus.PARTIALLY_FILLED,
            "canceled": OrderStatus.CANCELLED,
            "cancelled": OrderStatus.CANCELLED,
            "expired": OrderStatus.EXPIRED,
            "failure": OrderStatus.FAILED,
            "pending": OrderStatus.PENDING,
            "": OrderStatus.UNKNOWN,
            None: OrderStatus.UNKNOWN,
            "mystery-state": OrderStatus.UNKNOWN,
            OrderStatus.FILLED: OrderStatus.FILLED,
        }
        for raw, expected in table.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_order_status(raw), expected)

    def test_order_request_carries_time_in_force_and_execution_mode(self):
        default = OrderRequest("AAPL", Direction.LONG, 1, 10.0)
        self.assertEqual(default.time_in_force, "DAY")
        self.assertEqual(default.execution_mode, ExecutionMode.PAPER)
        tuned = OrderRequest("AAPL", Direction.LONG, 1, 10.0,
                             time_in_force=" gtc ", execution_mode="LIVE")
        self.assertEqual(tuned.time_in_force, "GTC")
        self.assertEqual(tuned.execution_mode, ExecutionMode.LIVE)
        with self.assertRaises(ValueError):
            OrderRequest("AAPL", Direction.LONG, 1, 10.0, time_in_force="  ")
        with self.assertRaises(ValueError):
            OrderRequest("AAPL", Direction.LONG, 1, 10.0, execution_mode="PAPER_ONLY")

    def test_provider_order_results_keep_the_provider_reference(self):
        adapter = ScriptedAdapter()
        request = OrderRequest("AAPL", Direction.LONG, 1, 10.0)
        placed = adapter.place_order(request)
        self.assertEqual(placed.status, OrderStatus.SUBMITTED)
        self.assertTrue(placed.provider_reference.startswith("ext-"))
        cancelled = adapter.cancel_order(placed.order_id)
        self.assertEqual(cancelled.status, OrderStatus.CANCELLED)
        self.assertEqual(cancelled.provider_reference, placed.provider_reference)

    def test_unrecognized_provider_status_becomes_pending_unknown(self):
        adapter = ScriptedAdapter(place_status="mystery-broker-word")
        order = adapter.place_order(OrderRequest("AAPL", Direction.LONG, 1, 10.0))
        self.assertEqual(order.status, OrderStatus.UNKNOWN)
        self.assertNotEqual(order.status, OrderStatus.FILLED)

    def test_order_defaults_stay_paper_and_are_serializable(self):
        order = Order("PAPER-1", OrderRequest("AAPL", Direction.LONG, 1, 10.0),
                      OrderStatus.SUBMITTED, 0, None, CLOCK)
        encoded = json.dumps(order, default=_json_default)
        self.assertIn("PAPER", encoded)
        self.assertIn("SUBMITTED", encoded)
        self.assertIn('"provider_reference": null', encoded)

    def test_local_paper_reconciliation_hook_returns_known_orders(self):
        clock = Clock()
        adapter = LocalPaperAdapter(cash=1_000.0, clock=clock)
        request = OrderRequest("AAPL", Direction.LONG, 1, 10.0)
        order = adapter.place_order(request)
        self.assertEqual(adapter.reconcile_orders(), (order,))
        self.assertEqual(adapter.reconcile_orders(since=clock() + timedelta(seconds=1)), ())
        self.assertEqual(adapter.reconcile_orders(since=clock() - timedelta(seconds=1)),
                         (order,))

    def test_health_probe_of_the_local_adapter_is_local_evidence_only(self):
        adapter = LocalPaperAdapter(cash=1_000.0, clock=Clock())
        health = adapter.health_check()
        self.assertTrue(health["success"])
        self.assertIn("no network request was made", health["detail"])
        adapter.disconnect()
        self.assertFalse(adapter.health_check()["success"])


class ConnectionCenterAndHttpTests(unittest.TestCase):
    def test_connection_center_reports_execution_connections_honestly(self):
        runtime = build_runtime()
        try:
            status = runtime.connections.status()
            section = status["execution_connections"]
            self.assertEqual(section["count"], 1)
            self.assertTrue(section["configured"])
            self.assertTrue(section["ready"])
            self.assertEqual(section["state"], "CONNECTED")
            paper = section["connections"][0]
            self.assertEqual(paper["provider_id"], "local_paper")
            self.assertEqual(paper["mode"], "PAPER")
            self.assertEqual(paper["state"], "CONNECTED")
            self.assertEqual(paper["authentication_state"], "AUTHENTICATED")
            self.assertEqual(paper["account_verification_state"], "VERIFIED")
            self.assertEqual(paper["permission_state"], "GRANTED")
            self.assertTrue(paper["ready"])
            provider_ids = {item["provider_id"] for item in section["providers"]}
            self.assertEqual(provider_ids, {"local_paper", "alpaca", "robinhood"})
            self.assertFalse(section["gate"]["live_allowed"])
            self.assertTrue(section["gate"]["paper_allowed"])
            self.assertFalse(section["gate"]["kill_switch_engaged"])
        finally:
            runtime.close()

    def test_execution_connection_test_passes_for_the_local_account_only(self):
        runtime = build_runtime()
        try:
            result = runtime.connections.test("execution")
            self.assertEqual(result["status"], "PASSED")
            self.assertTrue(result["success"])
            self.assertIn("no network request was made", result["message"])
            self.assertIn("paper:", result["message"])
        finally:
            runtime.close()

    def test_configured_external_provider_stays_unverified_and_secret_free(self):
        runtime = build_runtime(env={
            "TRADING_PLATFORM_PROVIDER": "alpaca",
            "ALPACA_API_KEY": SECRET,
            "ALPACA_API_SECRET": f"{SECRET}-two",
        })
        try:
            status = runtime.connections.status()
            section = status["execution_connections"]
            self.assertEqual(section["count"], 2)
            by_id = {item["connection_id"]: item for item in section["connections"]}
            client = by_id["client"]
            self.assertTrue(client["configured"])
            self.assertFalse(client["implemented"])
            self.assertEqual(client["state"], "CONFIGURED")
            self.assertFalse(client["ready"])
            self.assertEqual(client["account_verification_state"], "UNKNOWN")
            serialized = json.dumps(status, default=_json_default)
            self.assertNotIn(SECRET, serialized)
            readiness = runtime.connections.readiness()
            self.assertNotIn(SECRET, json.dumps(readiness, default=_json_default))
            self.assertEqual(readiness["live_mode"], "LOCKED")
            result = runtime.connections.test("execution")
            self.assertTrue(result["success"])
            self.assertIn("client: UNAVAILABLE", result["message"])
            client_result = runtime.connection_manager.verify("client")
            self.assertFalse(client_result.ready)
            self.assertIn("not installed",
                          runtime.connection_manager.get("client").describe()["reason"])
        finally:
            runtime.close()

    def test_runtime_execution_manager_is_wired_to_the_real_paper_adapter(self):
        runtime = build_runtime()
        try:
            self.assertIs(runtime.connection_manager.adapter_for("paper"),
                          runtime.paper_adapter)
            self.assertEqual(runtime.connection_manager.gate, runtime.execution_gate)
            self.assertFalse(runtime.execution_gate.describe()["live_allowed"])
            self.assertTrue(runtime.execution_providers.is_implemented("local_paper"))
        finally:
            runtime.close()

    def test_http_endpoints_report_execution_connections_without_secrets(self):
        runtime = build_runtime(env={
            "TRADING_PLATFORM_PROVIDER": "alpaca",
            "ALPACA_API_KEY": SECRET,
            "ALPACA_API_SECRET": f"{SECRET}-two",
        })
        server = create_server(runtime, "127.0.0.1", 0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path in ("/api/status", "/api/positions", "/api/readiness", "/api/connections"):
                document = self.get(port, path)
                self.assertIsInstance(document, dict, path)
                self.assertNotIn(SECRET, json.dumps(document, default=_json_default), path)
            connections = self.get(port, "/api/connections")
            section = connections["execution_connections"]
            self.assertEqual(section["count"], 2)
            self.assertEqual(section["gate"]["role"], "EXECUTION_SAFETY_GATE")
            readiness = self.get(port, "/api/readiness")
            self.assertTrue(any(item["key"] == "execution_connection_verified"
                                and item["passed"] for item in readiness["checks"]))
            # A configured but unverified external provider keeps the aggregate
            # at CONFIGURED; it never claims CONNECTED before verification.
            self.assertEqual(readiness["execution_state"], "CONFIGURED")
            self.assertEqual(readiness["execution_ready_count"], 1)
            status, result = self.post(port, "/api/connections/test",
                                       {"section": "execution"})
            self.assertEqual(status, 200)
            self.assertTrue(result["success"])
            self.assertNotIn(SECRET, json.dumps(result, default=_json_default))
            unauthenticated, _ = self.post(port, "/api/connections/test",
                                           {"section": "execution"}, token=None)
            self.assertEqual(unauthenticated, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            runtime.close()

    @staticmethod
    def get(port, path):
        with urlrequest.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def post(port, path, body, token=OWNER_TOKEN):
        request = urlrequest.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     **({"X-Owner-Token": token} if token else {})},
            method="POST")
        try:
            with urlrequest.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))


class FrontendSeparationTests(unittest.TestCase):
    def test_frontend_never_calls_a_vendor_or_absolute_endpoint(self):
        forbidden_hosts = (
            "alpaca.markets", "paper-api.alpaca", "api.robinhood.com", "robinhood.com",
            "polygon.io", "alphavantage.co",
            # No TradingView integration ships any more (retired 2026-10-12), but
            # the frontend must never grow a call to it either: this host stays
            # banned so a reintroduction fails the suite.
            "api.tradingview.com",
        )
        absolute_fetch = re.compile(r"fetch\(\s*['\"]https?://", re.IGNORECASE)
        sources = sorted(ROOT.joinpath("frontend", "src").rglob("*.ts*"))
        self.assertTrue(sources, "no frontend sources were found")
        for source in sources:
            text = source.read_text(encoding="utf-8")
            with self.subTest(file=source.name):
                for host in forbidden_hosts:
                    self.assertNotIn(host, text.lower())
                self.assertIsNone(absolute_fetch.search(text))

    def test_frontend_consumes_the_execution_connection_payload(self):
        panel = ROOT.joinpath("frontend", "src", "ConnectionsPanel.tsx").read_text(
            encoding="utf-8")
        self.assertIn("execution_connections", panel)


class RegressionGuardTests(unittest.TestCase):
    """Phase 1-5 behaviour must stay intact with the framework in place."""

    def test_market_data_domain_is_untouched_by_execution_connections(self):
        from backend.market_data.registry import is_implemented as market_implemented
        runtime = build_runtime()
        try:
            self.assertEqual(runtime.market_data.status["provider"], "none")
            self.assertEqual(runtime.market_data.status["state"], "NOT_CONFIGURED")
            self.assertTrue(market_implemented("polygon"))
            self.assertTrue(market_implemented("alphavantage"))
            self.assertEqual(runtime.market_data.get_latest(("AAPL",)), ())
            self.assertFalse(any(item["provider_id"] in {"polygon", "alphavantage"}
                                 for item in runtime.connections.status()
                                 ["execution_connections"]["connections"]))
        finally:
            runtime.close()

    def test_risk_engine_stays_between_decisions_and_execution(self):
        runtime = build_runtime()
        try:
            risk = runtime.risk
            self.assertGreaterEqual(risk.config.min_reward_risk, 2.0)
            self.assertFalse(risk.kill_switch_enabled)
            router = runtime.execution
            self.assertIsInstance(router, ExecutionRouter)
            self.assertEqual(router.identify(), "local_paper")
        finally:
            runtime.close()

    def test_execution_provider_registry_is_broker_agnostic(self):
        registry = default_execution_providers()
        for provider_id in ("local_paper", "alpaca", "robinhood"):
            with self.subTest(provider=provider_id):
                self.assertIsNotNone(registry.find(provider_id))
        # The framework never forces one broker: no provider is required to exist.
        optional = set(registry.ids())
        self.assertTrue(optional.issuperset({"alpaca", "robinhood"}))
        self.assertNotIn("local_paper", BROKER_CATALOG)

    def test_paper_adapter_fixture_is_not_promoted_to_a_broker_claim(self):
        adapter = MockTradingPlatformAdapter()
        self.assertEqual(adapter.identify(), "mock")
        descriptor = default_execution_providers().find("mock")
        self.assertIsNone(descriptor)


if __name__ == "__main__":
    unittest.main()
