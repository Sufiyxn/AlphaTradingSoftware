"""Normalized connection models and evidence-driven connection states.

States are recorded from evidence only: ``CONFIGURED`` means configuration
exists, ``CONNECTED`` means a verification actually succeeded, and a
configured-but-unverified connection stays ``CONFIGURED`` forever. Nothing in
this module stores, serialises, or logs a credential value.
"""
from __future__ import annotations

import threading
from datetime import datetime
from enum import Enum
from typing import Callable

from backend.execution.models import ExecutionMode

from .credentials import CredentialHandle

Clock = Callable[[], datetime]

NOT_CONFIGURED = "NOT_CONFIGURED"


class ConnectionState(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONFIGURED = "CONFIGURED"
    CONNECTING = "CONNECTING"
    AUTHENTICATED = "AUTHENTICATED"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"
    STALE = "STALE"


class AuthenticationState(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNVERIFIED = "UNVERIFIED"
    AUTHENTICATING = "AUTHENTICATING"
    AUTHENTICATED = "AUTHENTICATED"
    FAILED = "FAILED"


class AccountVerificationState(str, Enum):
    UNKNOWN = "UNKNOWN"
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class PermissionState(str, Enum):
    UNKNOWN = "UNKNOWN"
    GRANTED = "GRANTED"
    DENIED = "DENIED"


#: Capability vocabulary the platform understands. Unsupported entries show up
#: as capability *limitations* - never as invented support.
KNOWN_CAPABILITIES: tuple[str, ...] = (
    "equities", "fractional_shares", "market_orders", "limit_orders",
    "stop_orders", "stop_limit_orders", "short_selling", "extended_hours",
    "order_cancellation", "order_modification", "real_time_account_info",
    "positions", "order_status", "paper_trading", "live_trading", "market_data",
)


class ReadinessResult:
    """Normalized readiness answer for one connection.

    ``ready`` requires configuration, authentication, a successful connection
    verification, an account verification, trading permission for the mode,
    and an authorized execution mode - credentials alone are never enough.
    """

    __slots__ = (
        "connection_id", "provider_id", "mode", "ready", "configured",
        "authenticated", "connected", "account_verified", "trading_enabled",
        "state", "capability_limitations", "failure_reason", "checked_at",
    )

    def __init__(self, *, connection_id: str, provider_id: str, mode: ExecutionMode,
                 ready: bool, configured: bool, authenticated: bool, connected: bool,
                 account_verified: bool, trading_enabled: bool, state: str,
                 capability_limitations: tuple[str, ...] = (),
                 failure_reason: str | None = None,
                 checked_at: datetime | None = None) -> None:
        self.connection_id = connection_id
        self.provider_id = provider_id
        self.mode = mode
        self.ready = ready
        self.configured = configured
        self.authenticated = authenticated
        self.connected = connected
        self.account_verified = account_verified
        self.trading_enabled = trading_enabled
        self.state = state
        self.capability_limitations = tuple(capability_limitations)
        self.failure_reason = failure_reason
        self.checked_at = checked_at

    def describe(self) -> dict:
        return {
            "connection_id": self.connection_id,
            "provider_id": self.provider_id,
            "mode": self.mode.value,
            "ready": self.ready,
            "configured": self.configured,
            "authenticated": self.authenticated,
            "connected": self.connected,
            "account_verified": self.account_verified,
            "trading_enabled": self.trading_enabled,
            "state": self.state,
            "capability_limitations": list(self.capability_limitations),
            "failure_reason": self.failure_reason,
            "checked_at": self.checked_at,
        }

    def __repr__(self) -> str:
        return (f"ReadinessResult(connection_id={self.connection_id!r}, "
                f"ready={self.ready}, state={self.state!r})")


class ConnectionRecord:
    """Mutable, thread-safe evidence for one client execution connection."""

    __slots__ = (
        "_lock", "connection_id", "provider_id", "provider_label", "label", "mode",
        "environment", "known", "implemented", "configured", "configuration_reason",
        "required_credentials", "credentials", "stale_after_seconds", "_clock",
        "state", "reason", "authentication", "authentication_reason",
        "account_state", "account_reason", "permission", "permission_reason",
        "capabilities", "declared_capabilities", "capability_limitations",
        "health", "last_verified_at", "last_success_at", "last_error_at",
        "last_error", "last_readiness", "provider_instance_id",
    )

    def __init__(self, *, connection_id: str, provider_id: str, provider_label: str,
                 label: str, mode: ExecutionMode, environment: str | None,
                 known: bool, implemented: bool, configured: bool,
                 configuration_reason: str | None, required_credentials: tuple[str, ...],
                 credentials: CredentialHandle | None, clock: Clock,
                 stale_after_seconds: float = 300.0) -> None:
        self._lock = threading.Lock()
        self.connection_id = connection_id
        self.provider_id = provider_id
        self.provider_label = provider_label
        self.label = label
        self.mode = mode
        self.environment = environment
        self.known = known
        self.implemented = implemented
        self.configured = configured
        self.configuration_reason = configuration_reason
        self.required_credentials = tuple(required_credentials)
        self.credentials = credentials
        self.stale_after_seconds = float(stale_after_seconds)
        self._clock = clock
        self.state = (ConnectionState.CONFIGURED if configured
                      else ConnectionState.NOT_CONFIGURED)
        self.reason = configuration_reason
        self.authentication = (AuthenticationState.UNVERIFIED if configured
                               else AuthenticationState.NOT_CONFIGURED)
        self.authentication_reason = None
        self.account_state = AccountVerificationState.UNKNOWN
        self.account_reason = None
        self.permission = PermissionState.UNKNOWN
        self.permission_reason = None
        self.capabilities: tuple[str, ...] = ()
        self.declared_capabilities: tuple[str, ...] = ()
        self.capability_limitations: tuple[str, ...] = ()
        self.health: dict | None = None
        self.last_verified_at: datetime | None = None
        self.last_success_at: datetime | None = None
        self.last_error_at: datetime | None = None
        self.last_error: str | None = None
        self.last_readiness: ReadinessResult | None = None
        # Which provider instance this connection is running as. The
        # ConnectionManager binds it; provider order ids only mean anything
        # together with it, so it is part of every identity this connection
        # records.
        self.provider_instance_id: str | None = None

    def begin(self, attempted_at: datetime) -> None:
        with self._lock:
            self.state = ConnectionState.CONNECTING
            self.reason = "connection verification is in progress"
            self.last_verified_at = attempted_at

    def record_error(self, state: ConnectionState, reason: str, *,
                     at: datetime, authentication: AuthenticationState | None = None,
                     account: AccountVerificationState | None = None,
                     permission: PermissionState | None = None) -> None:
        with self._lock:
            self.state = state
            self.reason = reason
            self.last_error = reason
            self.last_error_at = at
            if authentication is not None:
                self.authentication = authentication
            if account is not None:
                self.account_state = account
            if permission is not None:
                self.permission = permission

    def record_success(self, *, at: datetime, state: ConnectionState, reason: str,
                       authentication: AuthenticationState,
                       account: AccountVerificationState,
                       permission: PermissionState, permission_reason: str | None,
                       capabilities: tuple[str, ...],
                       capability_limitations: tuple[str, ...],
                       health: dict | None,
                       authentication_reason: str | None = None,
                       account_reason: str | None = None) -> None:
        with self._lock:
            self.state = state
            self.reason = reason
            self.authentication = authentication
            self.authentication_reason = authentication_reason
            self.account_state = account
            self.account_reason = account_reason
            self.permission = permission
            self.permission_reason = permission_reason
            self.capabilities = tuple(capabilities)
            self.capability_limitations = tuple(capability_limitations)
            self.health = health
            self.last_verified_at = at
            self.last_success_at = at
            self.last_error = None
            self.last_error_at = None

    def invalidate(self, reason: str, *, at: datetime) -> None:
        """Drop verification evidence that described a different adapter.

        Called when the adapter behind this connection is replaced: every
        recorded success belonged to the previous instance, so the connection
        falls back to CONFIGURED until a fresh verification runs.
        """
        with self._lock:
            self.state = (ConnectionState.CONFIGURED if self.configured
                          else ConnectionState.NOT_CONFIGURED)
            self.reason = reason
            self.authentication = (AuthenticationState.UNVERIFIED if self.configured
                                   else AuthenticationState.NOT_CONFIGURED)
            self.authentication_reason = reason
            self.account_state = AccountVerificationState.UNKNOWN
            self.account_reason = None
            self.permission = PermissionState.UNKNOWN
            self.permission_reason = None
            self.capabilities = ()
            self.capability_limitations = ()
            self.health = None
            self.last_verified_at = None
            self.last_success_at = None
            self.last_error_at = at
            self.last_error = reason
            self.last_readiness = None

    def current_state(self) -> tuple[ConnectionState, str | None]:
        """Recorded state, downgrading expired success evidence to ``STALE``."""
        with self._lock:
            state, reason = self.state, self.reason
            if state is ConnectionState.CONNECTED and self.last_success_at is not None:
                age = (self._clock() - self.last_success_at).total_seconds()
                if age > self.stale_after_seconds:
                    state = ConnectionState.STALE
                    reason = ("the last successful verification is older than "
                              f"{self.stale_after_seconds:g} seconds")
            return state, reason

    def describe(self) -> dict:
        """Secret-free view of this connection for the API and dashboard."""
        state, reason = self.current_state()
        readiness = self.last_readiness.describe() if self.last_readiness else None
        handle = self.credentials
        return {
            "connection_id": self.connection_id,
            "provider_id": self.provider_id,
            "provider_instance_id": self.provider_instance_id,
            "provider_label": self.provider_label,
            "label": self.label,
            "mode": self.mode.value,
            "environment": self.environment,
            "known": self.known,
            "implemented": self.implemented,
            "configured": self.configured,
            "configuration_reason": self.configuration_reason,
            "state": state.value,
            "reason": reason,
            "authentication_state": self.authentication.value,
            "authentication_reason": self.authentication_reason,
            "account_verification_state": self.account_state.value,
            "account_verification_reason": self.account_reason,
            "permission_state": self.permission.value,
            "permission_reason": self.permission_reason,
            "capabilities": list(self.capabilities),
            "declared_capabilities": list(self.declared_capabilities),
            "capability_limitations": list(self.capability_limitations),
            "health": dict(self.health) if self.health else None,
            "ready": bool(self.last_readiness and self.last_readiness.ready),
            "failure_reason": (self.last_readiness.failure_reason
                               if self.last_readiness else None),
            "readiness": readiness,
            "last_verified_at": self.last_verified_at,
            "last_success_at": self.last_success_at,
            "last_error_at": self.last_error_at,
            "last_error": self.last_error,
            "credential_fields": (handle.describe(self.required_credentials)
                                  if handle is not None else [
                                      {"name": name, "configured": False}
                                      for name in self.required_credentials]),
        }
