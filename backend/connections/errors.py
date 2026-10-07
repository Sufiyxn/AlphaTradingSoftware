"""Classified execution-provider failures.

Every adapter failure surfaces as one of these types so the connection
manager can record an honest state instead of a fabricated one. Messages must
already be secret-free; the manager additionally redacts them against the
connection's credential handle before anything is stored or reported.
"""
from __future__ import annotations


class ExecutionProviderError(Exception):
    """Base class for execution-provider failures."""


class UnknownExecutionProviderError(ExecutionProviderError):
    """The requested provider id is not in the execution provider registry."""


class ExecutionNotInstalledError(ExecutionProviderError):
    """The provider is known but this build ships no adapter for it."""


class ExecutionConfigurationError(ExecutionProviderError):
    """The connection is missing required configuration or credentials."""


class ExecutionAuthenticationError(ExecutionProviderError):
    """The provider rejected or could not confirm the credentials."""


class ExecutionConnectionError(ExecutionProviderError):
    """The provider could not be reached."""


class ExecutionTimeoutError(ExecutionConnectionError):
    """The provider did not answer within the timeout."""


class ExecutionRateLimitError(ExecutionProviderError):
    """The provider throttled the request."""


class ExecutionAccountError(ExecutionProviderError):
    """The provider answered but the account could not be verified."""


class ExecutionOrderError(ExecutionProviderError):
    """The provider rejected an order operation."""
