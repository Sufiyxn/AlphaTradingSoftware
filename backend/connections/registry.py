"""Execution provider registry - which providers this build can talk to.

Adding a provider means registering a descriptor with an adapter factory that
implements ``TradingPlatformAdapter``. Nothing else changes: strategy code,
the risk engine, the decision engine, the signal paths, and the market-data
providers never learn a provider's name.

``implemented=False`` descriptors are advertised honestly - they have no
adapter factory, so :meth:`ExecutionProviderRegistry.build` refuses them
instead of returning something that only pretends to work.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from backend.config.catalog import BROKER_CATALOG
from backend.execution.local_paper import LocalPaperAdapter
from backend.execution.models import ExecutionMode

from .credentials import CredentialHandle
from .errors import ExecutionNotInstalledError, UnknownExecutionProviderError

AUTHENTICATION_MECHANISMS = ("NONE", "API_KEY", "API_KEY_SECRET", "OAUTH", "UNDOCUMENTED")

AdapterFactory = Callable[..., object]


@dataclass(frozen=True, slots=True)
class ExecutionProviderDescriptor:
    """Static description of one execution provider."""

    provider_id: str
    label: str
    implemented: bool
    adapter_factory: AdapterFactory | None = None
    authentication: str = "NONE"
    supported_modes: tuple[ExecutionMode, ...] = (ExecutionMode.PAPER,)
    asset_classes: tuple[str, ...] = ("equities",)
    declared_capabilities: tuple[str, ...] = ()
    credential_fields: tuple[str, ...] = ()
    environments: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        provider_id = str(self.provider_id or "").strip().lower()
        if not provider_id or not all(part.isalnum() for part in
                                      provider_id.replace("-", "_").split("_")):
            raise ValueError(f"invalid execution provider id: {self.provider_id!r}")
        object.__setattr__(self, "provider_id", provider_id)
        if self.implemented and not callable(self.adapter_factory):
            raise ValueError(
                f"{provider_id} is marked implemented but provides no adapter factory")
        if not self.implemented and self.adapter_factory is not None:
            raise ValueError(
                f"{provider_id} provides an adapter factory but is not marked implemented")
        if self.authentication not in AUTHENTICATION_MECHANISMS:
            raise ValueError(
                f"unsupported authentication mechanism {self.authentication!r} for {provider_id}")
        modes = tuple(self.supported_modes)
        if any(not isinstance(mode, ExecutionMode) for mode in modes):
            raise ValueError(f"{provider_id} declared a non-execution mode")
        object.__setattr__(self, "supported_modes", modes)

    @property
    def requires_credentials(self) -> bool:
        return bool(self.credential_fields)

    def build(self, *, credentials: CredentialHandle | None = None, clock=None,
              mode: ExecutionMode | None = None, environment: str | None = None,
              **factory_kwargs) -> object:
        if not self.implemented or self.adapter_factory is None:
            raise ExecutionNotInstalledError(
                f"the {self.label} execution adapter is not installed; "
                "no network request was made")
        return self.adapter_factory(credentials=credentials, clock=clock, mode=mode,
                                    environment=environment, **factory_kwargs)

    def describe(self) -> dict:
        """Provider metadata for the API - never a callable, never a secret."""
        return {
            "provider_id": self.provider_id,
            "label": self.label,
            "implemented": self.implemented,
            "authentication": self.authentication,
            "supported_modes": [mode.value for mode in self.supported_modes],
            "asset_classes": list(self.asset_classes),
            "declared_capabilities": list(self.declared_capabilities),
            "credential_fields": [{"name": name, "configured": False}
                                  for name in self.credential_fields],
            "environments": list(self.environments),
            "metadata": {key: value for key, value in self.metadata},
        }


class ExecutionProviderRegistry:
    """Named registry of execution provider descriptors."""

    def __init__(self, descriptors: Iterable[ExecutionProviderDescriptor] = ()) -> None:
        self._descriptors: dict[str, ExecutionProviderDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: ExecutionProviderDescriptor) -> None:
        if not isinstance(descriptor, ExecutionProviderDescriptor):
            raise TypeError("only execution provider descriptors can be registered")
        if descriptor.provider_id in self._descriptors:
            raise ValueError(f"execution provider already registered: {descriptor.provider_id}")
        self._descriptors[descriptor.provider_id] = descriptor

    def find(self, provider_id: str) -> ExecutionProviderDescriptor | None:
        return self._descriptors.get(str(provider_id or "").strip().lower())

    def get(self, provider_id: str) -> ExecutionProviderDescriptor:
        found = self.find(provider_id)
        if found is None:
            raise UnknownExecutionProviderError(
                f"unknown execution provider: {provider_id}")
        return found

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._descriptors))

    def descriptors(self) -> tuple[ExecutionProviderDescriptor, ...]:
        return tuple(self._descriptors[key] for key in sorted(self._descriptors))

    def is_implemented(self, provider_id: str) -> bool:
        found = self.find(provider_id)
        return bool(found and found.implemented)

    def build(self, provider_id: str, *, credentials: CredentialHandle | None = None,
              clock=None, mode: ExecutionMode | None = None,
              environment: str | None = None, **factory_kwargs) -> object:
        return self.get(provider_id).build(credentials=credentials, clock=clock, mode=mode,
                                           environment=environment, **factory_kwargs)

    def describe(self) -> list[dict]:
        return [descriptor.describe() for descriptor in self.descriptors()]


def _build_local_paper(*, credentials=None, clock=None, mode=None, environment=None,
                       cash: float = 100_000.0, **_ignored) -> LocalPaperAdapter:
    kwargs: dict = {"cash": cash}
    if clock is not None:
        kwargs["clock"] = clock
    return LocalPaperAdapter(**kwargs)


def _alpaca_declared_capabilities() -> tuple[str, ...]:
    entry = BROKER_CATALOG.get("alpaca")
    return tuple(entry.capabilities) if entry else ()


def default_execution_providers() -> ExecutionProviderRegistry:
    """A fresh registry: the paper account this build ships plus advertised,
    deliberately unimplemented external providers."""
    return ExecutionProviderRegistry((
        ExecutionProviderDescriptor(
            provider_id="local_paper",
            label="Local paper account",
            implemented=True,
            adapter_factory=_build_local_paper,
            authentication="NONE",
            supported_modes=(ExecutionMode.PAPER,),
            asset_classes=("equities",),
            declared_capabilities=(),
            credential_fields=(),
            environments=("PAPER",),
            metadata=(("role", "in-process paper execution"),
                      ("network", "none; this adapter performs no requests")),
        ),
        ExecutionProviderDescriptor(
            provider_id="alpaca",
            label="Alpaca",
            implemented=False,
            authentication="API_KEY_SECRET",
            supported_modes=(ExecutionMode.PAPER, ExecutionMode.LIVE),
            asset_classes=("equities",),
            declared_capabilities=_alpaca_declared_capabilities(),
            credential_fields=("ALPACA_API_KEY", "ALPACA_API_SECRET"),
            environments=("SANDBOX", "LIVE"),
            metadata=(("status", "not implemented in this build"),
                      ("verification", "declared capabilities are advertised, not verified")),
        ),
        ExecutionProviderDescriptor(
            provider_id="robinhood",
            label="Robinhood",
            implemented=False,
            authentication="UNDOCUMENTED",
            supported_modes=(),
            asset_classes=(),
            declared_capabilities=(),
            credential_fields=(),
            environments=(),
            metadata=(("status", "example future provider; no adapter and no "
                                 "documented integration exists in this build"),),
        ),
    ))
