"""Provider-agnostic client connection framework (Phase 6).

Three independent domains stay separate: market-data providers, signal /
analysis sources, and execution providers. This package owns only the third -
connection configuration, verification, capability discovery, readiness, and
the normalized vocabulary every future execution adapter must satisfy.
"""

from .credentials import CredentialHandle
from .errors import (
    ExecutionAccountError,
    ExecutionAuthenticationError,
    ExecutionConfigurationError,
    ExecutionConnectionError,
    ExecutionNotInstalledError,
    ExecutionOrderError,
    ExecutionProviderError,
    ExecutionRateLimitError,
    ExecutionTimeoutError,
    UnknownExecutionProviderError,
)
from .manager import ConnectionManager
from .models import (
    KNOWN_CAPABILITIES,
    AccountVerificationState,
    AuthenticationState,
    ConnectionRecord,
    ConnectionState,
    PermissionState,
    ReadinessResult,
)
from .registry import (
    ExecutionProviderDescriptor,
    ExecutionProviderRegistry,
    default_execution_providers,
)

__all__ = [
    "KNOWN_CAPABILITIES",
    "AccountVerificationState",
    "AuthenticationState",
    "ConnectionManager",
    "ConnectionRecord",
    "ConnectionState",
    "CredentialHandle",
    "ExecutionAccountError",
    "ExecutionAuthenticationError",
    "ExecutionConfigurationError",
    "ExecutionConnectionError",
    "ExecutionNotInstalledError",
    "ExecutionOrderError",
    "ExecutionProviderDescriptor",
    "ExecutionProviderError",
    "ExecutionProviderRegistry",
    "ExecutionRateLimitError",
    "ExecutionTimeoutError",
    "PermissionState",
    "ReadinessResult",
    "UnknownExecutionProviderError",
    "default_execution_providers",
]
