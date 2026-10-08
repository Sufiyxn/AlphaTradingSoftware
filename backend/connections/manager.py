"""Provider-agnostic Connection Manager.

Responsibilities: connection configurations, provider identification,
authentication, connectivity checks, account verification, permission checks,
capability discovery, readiness evaluation, connection health, credential
handling, and multiple simultaneous client connections.

Deliberately absent: strategy logic, risk logic, and broker-specific logic.
``RiskEngine`` stays between decision generation and execution; this manager
only answers "is this connection verified and usable?".
"""
from __future__ import annotations

import re
import threading

from backend.execution.gate import ExecutionSafetyGate
from backend.execution.identity import ConnectionInstanceRegistry, ProviderInstanceIdentity
from backend.execution.models import ExecutionMode

from .credentials import CredentialHandle
from .errors import (
    ExecutionAccountError,
    ExecutionAuthenticationError,
    ExecutionConnectionError,
    ExecutionProviderError,
    ExecutionRateLimitError,
    ExecutionTimeoutError,
)
from .models import (
    AccountVerificationState,
    AuthenticationState,
    ConnectionRecord,
    ConnectionState,
    KNOWN_CAPABILITIES,
    PermissionState,
    ReadinessResult,
)
from .registry import ExecutionProviderRegistry, default_execution_providers

CONNECTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Aggregate reporting order: problems surface before healthy states.
_STATE_PRECEDENCE: tuple[ConnectionState, ...] = (
    ConnectionState.ERROR,
    ConnectionState.UNAVAILABLE,
    ConnectionState.DEGRADED,
    ConnectionState.STALE,
    ConnectionState.CONFIGURED,
    ConnectionState.CONNECTING,
    ConnectionState.AUTHENTICATED,
    ConnectionState.CONNECTED,
    ConnectionState.NOT_CONFIGURED,
)


class ConnectionManager:
    """Owns client execution connections and their verification lifecycle."""

    def __init__(self, *, clock, registry: ExecutionProviderRegistry | None = None,
                 stale_after_seconds: float = 300.0,
                 gate: ExecutionSafetyGate | None = None,
                 factory_kwargs: dict | None = None,
                 journal=None) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self.registry = registry or default_execution_providers()
        self._clock = clock
        self._stale_after_seconds = float(stale_after_seconds)
        self.gate = gate or ExecutionSafetyGate()
        self._factory_kwargs = dict(factory_kwargs or {})
        self._records: dict[str, ConnectionRecord] = {}
        self._adapters: dict[str, object] = {}
        self._lock = threading.RLock()
        #: Provider-instance identity per connection. With a journal the
        #: instance id and its order sequence survive a restart; without one
        #: each process still mints a fresh instance so two runs can never
        #: hand out the same provider order identity.
        self.instances = ConnectionInstanceRegistry(journal)

    # ------------------------------------------------------------------ setup
    def add(self, *, connection_id: str, provider_id: str, label: str | None = None,
            mode: ExecutionMode = ExecutionMode.PAPER, environment: str | None = None,
            credentials: CredentialHandle | None = None, adapter=None,
            verify: bool | None = None) -> ConnectionRecord:
        """Register one client connection. Multiple connections are supported."""
        connection_id = str(connection_id or "").strip()
        if not CONNECTION_ID_PATTERN.match(connection_id):
            raise ValueError(
                "connection id must be 1-64 characters of letters, digits, "
                "'.', '-' or '_' and start with a letter or digit")
        provider_id = str(provider_id or "").strip().lower()
        if not isinstance(mode, ExecutionMode):
            mode = ExecutionMode(str(mode).strip().upper())
        descriptor = self.registry.find(provider_id)
        with self._lock:
            if connection_id in self._records:
                raise ValueError(f"connection already exists: {connection_id}")
            known = descriptor is not None
            required = tuple(descriptor.credential_fields) if descriptor else ()
            missing = (credentials.missing(required) if credentials is not None
                       else required)
            if adapter is not None:
                configured, reason = True, None
            elif not known:
                configured, reason = False, f"{provider_id} is not a known execution provider"
            elif missing:
                configured = False
                reason = f"credentials are missing: {', '.join(missing)}"
            else:
                configured, reason = True, None
            record = ConnectionRecord(
                connection_id=connection_id,
                provider_id=provider_id,
                provider_label=descriptor.label if descriptor else provider_id,
                label=str(label or (descriptor.label if descriptor else provider_id)),
                mode=mode,
                environment=(str(environment).strip().upper() or None) if environment else None,
                known=known,
                implemented=bool(descriptor and descriptor.implemented),
                configured=configured,
                configuration_reason=reason,
                required_credentials=required,
                credentials=credentials,
                clock=self._clock,
                stale_after_seconds=self._stale_after_seconds,
            )
            if descriptor is not None:
                record.declared_capabilities = tuple(descriptor.declared_capabilities)
            self._records[connection_id] = record
            if adapter is not None:
                self._adapters[connection_id] = adapter
        if verify is None:
            # Local in-process providers verify at registration; anything that
            # would talk to an external provider waits for an explicit test.
            verify = adapter is not None or (
                descriptor is not None and descriptor.authentication == "NONE")
        self._bind_instance(connection_id, adapter)
        if verify:
            self.verify(connection_id)
        return record

    def _bind_instance(self, connection_id: str,
                       adapter: object | None = None) -> ProviderInstanceIdentity:
        """Bind this connection's provider instance onto its record and adapter.

        A provider order number means nothing on its own: it is only unique
        for the instance that minted it. The instance id is therefore decided
        here - durably when a journal exists - and handed to the adapter so
        every identity it mints, and every identity the pipeline records for
        this connection, carries it.
        """
        identity = self.instances.bind(connection_id)
        record = self._records.get(connection_id)
        if record is not None:
            record.provider_instance_id = identity.instance_id
        if adapter is None:
            with self._lock:
                adapter = self._adapters.get(connection_id)
        if adapter is None:
            return identity
        binder = getattr(adapter, "bind_provider_identity", None)
        if callable(binder):
            binder(identity)
        else:
            try:
                adapter.provider_instance_id = identity.instance_id
            except (AttributeError, TypeError):
                pass  # an adapter that cannot carry one still orders normally
        return identity

    def get(self, connection_id: str) -> ConnectionRecord:
        try:
            return self._records[connection_id]
        except KeyError:
            raise KeyError(f"unknown connection: {connection_id}") from None

    def records(self) -> tuple[ConnectionRecord, ...]:
        with self._lock:
            return tuple(self._records.values())

    def connection_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._records)

    def adapter_for(self, connection_id: str) -> object | None:
        """The verified adapter instance, if one has been built."""
        with self._lock:
            return self._adapters.get(connection_id)

    def set_adapter(self, connection_id: str, adapter: object) -> None:
        """Replace the adapter behind an existing connection.

        Verification evidence always described the previous instance, so it is
        dropped here; the connection reports CONFIGURED until the pipeline's
        next verification re-establishes honest readiness for the new adapter.
        """
        record = self.get(connection_id)
        with self._lock:
            self._adapters[connection_id] = adapter
        # The new instance mints its own identities from this connection's
        # durable instance id, so replacing an adapter never re-uses numbers.
        self._bind_instance(connection_id, adapter)
        record.invalidate(
            "the execution adapter was replaced and has not been verified yet",
            at=self._clock())

    # ------------------------------------------------------------ verification
    def verify(self, connection_id: str) -> ReadinessResult:
        """Run the full verification lifecycle and record honest evidence."""
        record = self.get(connection_id)
        attempted = self._clock()
        record.begin(attempted)
        handle = record.credentials

        def fail(state: ConnectionState, reason: str, *,
                 authentication: AuthenticationState | None = None,
                 account: AccountVerificationState | None = None,
                 permission: PermissionState | None = None) -> ReadinessResult:
            record.record_error(state, self._redact(handle, reason), at=attempted,
                                authentication=authentication, account=account,
                                permission=permission)
            return self.readiness(connection_id)

        descriptor = self.registry.find(record.provider_id)
        if descriptor is None:
            return fail(ConnectionState.ERROR,
                        f"{record.provider_id} is not a known execution provider",
                        authentication=AuthenticationState.NOT_CONFIGURED)
        if not record.configured:
            return fail(ConnectionState.NOT_CONFIGURED,
                        record.configuration_reason or "the connection is not configured",
                        authentication=AuthenticationState.NOT_CONFIGURED)

        adapter = self._adapters.get(connection_id)
        if adapter is None:
            missing = (handle.missing(descriptor.credential_fields)
                       if handle is not None else descriptor.credential_fields)
            if descriptor.adapter_factory is None:
                return fail(ConnectionState.UNAVAILABLE,
                            f"the {descriptor.label} execution adapter is not installed; "
                            "no network request was made")
            if missing:
                return fail(ConnectionState.NOT_CONFIGURED,
                            f"credentials are missing: {', '.join(missing)}; "
                            "no network request was made",
                            authentication=AuthenticationState.NOT_CONFIGURED)
            try:
                adapter = descriptor.build(
                    credentials=handle, clock=self._clock, mode=record.mode,
                    environment=record.environment, **self._factory_kwargs)
            except ExecutionAuthenticationError as error:
                return fail(ConnectionState.ERROR, f"authentication failed: {error}",
                            authentication=AuthenticationState.FAILED)
            except ExecutionRateLimitError as error:
                return fail(ConnectionState.DEGRADED, f"rate limited: {error}",
                            authentication=AuthenticationState.UNVERIFIED)
            except (ExecutionTimeoutError, ExecutionConnectionError) as error:
                return fail(ConnectionState.UNAVAILABLE, str(error),
                            authentication=AuthenticationState.UNVERIFIED)
            except ExecutionProviderError as error:
                return fail(ConnectionState.ERROR, str(error),
                            authentication=AuthenticationState.FAILED)
            except Exception as error:  # noqa: BLE001 - adapter bugs surface honestly
                return fail(ConnectionState.ERROR,
                            f"the provider adapter could not be initialised: {error}",
                            authentication=AuthenticationState.FAILED)
            with self._lock:
                self._adapters[connection_id] = adapter
        # Re-bind every time the adapter is (re)established: whichever
        # process verifies owns the instance this connection mints under.
        self._bind_instance(connection_id, adapter)

        # --- authentication -------------------------------------------------
        if descriptor.authentication == "NONE":
            authentication = AuthenticationState.AUTHENTICATED
            authentication_reason = "this provider requires no credentials (in-process account)"
        else:
            record.authentication = AuthenticationState.AUTHENTICATING
            record.authentication_reason = "authenticating with the provider"
            try:
                adapter.connect()
            except ExecutionAuthenticationError as error:
                return fail(ConnectionState.ERROR, f"authentication failed: {error}",
                            authentication=AuthenticationState.FAILED)
            except ExecutionRateLimitError as error:
                return fail(ConnectionState.DEGRADED, f"rate limited: {error}",
                            authentication=AuthenticationState.UNVERIFIED)
            except (ExecutionTimeoutError, ExecutionConnectionError) as error:
                return fail(ConnectionState.UNAVAILABLE, str(error),
                            authentication=AuthenticationState.UNVERIFIED)
            except ExecutionProviderError as error:
                return fail(ConnectionState.ERROR, str(error),
                            authentication=AuthenticationState.FAILED)
            except Exception as error:  # noqa: BLE001
                return fail(ConnectionState.ERROR,
                            f"the provider could not be contacted: {error}",
                            authentication=AuthenticationState.FAILED)
            if not bool(adapter.is_authenticated()):
                return fail(ConnectionState.ERROR,
                            "the provider did not confirm authentication",
                            authentication=AuthenticationState.FAILED)
            authentication = AuthenticationState.AUTHENTICATED
            authentication_reason = None

        # --- capability discovery ------------------------------------------
        try:
            capabilities = adapter.capabilities()
        except Exception as error:  # noqa: BLE001
            return fail(ConnectionState.ERROR, f"capability discovery failed: {error}",
                        authentication=authentication)
        supported = tuple(capabilities.names())
        limitations = tuple(name for name in KNOWN_CAPABILITIES if name not in supported)
        record.capabilities = supported
        record.capability_limitations = limitations

        # --- account verification ------------------------------------------
        if not capabilities.account_retrieval:
            account_state = AccountVerificationState.UNSUPPORTED
            account_reason = "this provider does not expose account verification"
            account_ok = False
        else:
            try:
                snapshot = adapter.get_account()
            except (ExecutionAccountError, ExecutionProviderError) as error:
                return fail(ConnectionState.ERROR,
                            f"account verification failed: {error}",
                            authentication=authentication,
                            account=AccountVerificationState.FAILED)
            except Exception as error:  # noqa: BLE001
                return fail(ConnectionState.ERROR,
                            f"account verification failed: {error}",
                            authentication=authentication,
                            account=AccountVerificationState.FAILED)
            if snapshot is None:
                return fail(ConnectionState.ERROR,
                            "the provider returned no account snapshot",
                            authentication=authentication,
                            account=AccountVerificationState.FAILED)
            account_state = AccountVerificationState.VERIFIED
            account_reason = None
            account_ok = True

        # --- permission check ----------------------------------------------
        supported_mode = (capabilities.paper_trading if record.mode is ExecutionMode.PAPER
                          else capabilities.live_trading)
        if supported_mode:
            permission = PermissionState.GRANTED
            permission_reason = None
            permission_ok = True
        else:
            permission = PermissionState.DENIED
            permission_reason = (f"the provider does not support "
                                 f"{record.mode.value.lower()} trading")
            permission_ok = False

        # --- provider health -------------------------------------------------
        health: dict = {"success": None, "status": "UNKNOWN",
                        "detail": "the provider exposes no health probe"}
        health_ok = True
        probe = getattr(adapter, "health_check", None)
        if callable(probe):
            try:
                health = dict(probe() or {})
            except Exception as error:  # noqa: BLE001
                health = {"success": False, "status": "ERROR",
                          "detail": f"the health probe failed: {error}"}
            for key, value in tuple(health.items()):
                if isinstance(value, str):
                    health[key] = self._redact(handle, value)
            health_ok = health.get("success") is not False

        # --- finalize --------------------------------------------------------
        mode_allowed, mode_reason = self.gate.authorize(record.mode)
        local = descriptor.authentication == "NONE"
        if not health_ok:
            state = ConnectionState.DEGRADED
            reason = str(health.get("detail") or "the provider reported a degraded state")
        else:
            state = ConnectionState.CONNECTED
            reason = ("the local in-process account answered the verification; "
                      "no external provider was contacted" if local else
                      "the provider answered the verification request")
        if not permission_ok:
            reason = f"{reason}; {permission_reason}"
        if not mode_allowed:
            reason = f"{reason}; {mode_reason}"
        record.record_success(
            at=attempted, state=state, reason=reason, authentication=authentication,
            account=account_state, permission=permission,
            permission_reason=permission_reason, capabilities=supported,
            capability_limitations=limitations, health=health,
            authentication_reason=authentication_reason, account_reason=account_reason)
        return self.readiness(connection_id)

    def verify_all(self) -> tuple[ReadinessResult, ...]:
        return tuple(self.verify(connection_id) for connection_id in self.connection_ids())

    # --------------------------------------------------------------- readiness
    def readiness(self, connection_id: str) -> ReadinessResult:
        """Readiness from recorded evidence; credentials alone never pass."""
        record = self.get(connection_id)
        state, state_reason = record.current_state()
        authenticated = record.authentication is AuthenticationState.AUTHENTICATED
        account_verified = record.account_state is AccountVerificationState.VERIFIED
        connected = state is ConnectionState.CONNECTED
        permission_ok = record.permission is PermissionState.GRANTED
        mode_allowed, mode_reason = self.gate.authorize(record.mode)
        trading_enabled = permission_ok and mode_allowed

        failure: str | None = None
        if not record.configured:
            failure = record.configuration_reason or "the connection is not configured"
        elif state in (ConnectionState.ERROR, ConnectionState.UNAVAILABLE,
                       ConnectionState.DEGRADED, ConnectionState.NOT_CONFIGURED):
            failure = state_reason or "the connection verification did not succeed"
        elif not authenticated:
            failure = record.authentication_reason or "authentication has not been confirmed"
        elif not connected:
            failure = (state_reason if state is ConnectionState.STALE else
                       "the connection has not completed a successful verification")
        elif not account_verified:
            failure = (record.account_reason or
                       "the account has not been verified")
        elif not permission_ok:
            failure = (record.permission_reason or
                       "trading permission has not been granted for this mode")
        elif not mode_allowed:
            failure = mode_reason

        result = ReadinessResult(
            connection_id=record.connection_id,
            provider_id=record.provider_id,
            mode=record.mode,
            ready=(record.configured and authenticated and connected and
                   account_verified and trading_enabled),
            configured=record.configured,
            authenticated=authenticated,
            connected=connected,
            account_verified=account_verified,
            trading_enabled=trading_enabled,
            state=state.value,
            capability_limitations=record.capability_limitations,
            failure_reason=failure,
            checked_at=record.last_verified_at,
        )
        record.last_readiness = result
        return result

    # ---------------------------------------------------------------- reporting
    def aggregate_state(self) -> tuple[str, str | None]:
        """One honest state for the whole set of connections."""
        records = self.records()
        if not records:
            return ConnectionState.NOT_CONFIGURED.value, "no execution connection is configured"
        for candidate in _STATE_PRECEDENCE:
            for record in records:
                state, reason = record.current_state()
                if state is candidate:
                    return state.value, reason
        return ConnectionState.NOT_CONFIGURED.value, "no execution connection is configured"

    def describe(self) -> dict:
        """Secret-free report used by the Connection Center and the API."""
        records = self.records()
        state, reason = self.aggregate_state()
        results = [record.last_readiness for record in records]
        return {
            "count": len(records),
            "configured": any(record.configured for record in records),
            "ready": any(result is not None and result.ready for result in results),
            "ready_count": sum(1 for result in results if result is not None and result.ready),
            "state": state,
            "reason": reason,
            "connections": [record.describe() for record in records],
            "providers": self.registry.describe(),
            "gate": self.gate.describe(),
        }

    @staticmethod
    def _redact(handle: CredentialHandle | None, text: object) -> str:
        if text is None:
            return ""
        return handle.redact(text) if handle is not None else str(text)
