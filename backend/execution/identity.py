"""Durable provider-instance identity for execution connections.

A provider order identity is never just its number. It is the triple

    ``connection_id + provider_instance_id + provider_order_id``

where ``provider_instance_id`` names *which* connection instance minted the
number. A process restart is exactly when a provider is most likely to hand
out a number it handed out before, so the instance id is what keeps the old
order and the new one apart.

Two levels of durability, in order of preference:

* with a journal, the instance id and the order sequence live in the
  ``connection_instances`` table: both survive a restart, so ids keep their
  meaning across processes and a restarted run continues the sequence rather
  than repeating it;
* without a journal the registry still mints a *fresh* instance id per
  process. The sequence is then process-local, but two runs can never share
  a prefix, so a restarted process cannot re-issue an identity it used
  before.

Nothing here talks to a provider, a network, or a market. It only answers
"which instance is this connection, and what is the next order number it
may hand out".
"""
from __future__ import annotations

import itertools
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

__all__ = [
    "ConnectionInstanceRegistry",
    "ProviderInstanceIdentity",
    "ProviderInstanceMixin",
    "mint_instance_id",
]

#: The journal methods a repository must expose to make identities durable.
_DURABLE_METHODS = ("connection_instance", "bind_connection_instance",
                    "next_provider_sequence")


def mint_instance_id() -> str:
    """A short, opaque id identifying one instance of an execution connection."""
    return uuid.uuid4().hex[:8]


@dataclass(frozen=True)
class ProviderInstanceIdentity:
    """What a connection knows about the instance it is currently running as."""

    connection_id: str
    instance_id: str
    #: Allocates the next durable provider-order sequence for this connection.
    #: ``None`` when no journal exists and the sequence is process-local.
    next_sequence: Callable[[], int] | None = None


class ConnectionInstanceRegistry:
    """Binds each connection id to one durable provider instance id.

    The first ``bind`` of a connection creates the identity; every later
    ``bind`` - including one from a new process over the same journal -
    recovers it. Identity is never silently re-minted while a journal can
    still answer.
    """

    def __init__(self, journal: Any | None = None) -> None:
        self._journal = journal if _supports_durable_instances(journal) else None
        self._lock = threading.RLock()
        self._bound: dict[str, ProviderInstanceIdentity] = {}

    @property
    def durable(self) -> bool:
        """True when instance ids and sequences survive a restart."""
        return self._journal is not None

    def bind(self, connection_id: str) -> ProviderInstanceIdentity:
        """The identity for ``connection_id``, creating it if this is its first use."""
        connection_id = str(connection_id or "").strip()
        if not connection_id:
            raise ValueError("a provider instance identity needs a connection id")
        with self._lock:
            cached = self._bound.get(connection_id)
            if cached is not None:
                return cached
            identity = (self._bind_durable(connection_id) if self._journal is not None
                        else self._bind_process_local(connection_id))
            self._bound[connection_id] = identity
            return identity

    def _bind_process_local(self, connection_id: str) -> ProviderInstanceIdentity:
        counter = itertools.count(1)
        return ProviderInstanceIdentity(connection_id, mint_instance_id(),
                                        lambda: next(counter))

    def _bind_durable(self, connection_id: str) -> ProviderInstanceIdentity:
        journal = self._journal
        row = _row(journal.connection_instance(connection_id))
        if row is None:
            try:
                row = _row(journal.bind_connection_instance(
                    connection_id, instance_id=mint_instance_id(),
                    created_at=datetime.now(timezone.utc)))
            except Exception:  # noqa: BLE001 - a lost race still gets an answer
                row = _row(journal.connection_instance(connection_id))
        if row is None:
            # The journal stopped answering: fall back to a process-local
            # identity rather than pretending the durable one still holds.
            return self._bind_process_local(connection_id)
        return ProviderInstanceIdentity(
            connection_id, row,
            lambda: journal.next_provider_sequence(connection_id))


def _row(answer: Any) -> str | None:
    if isinstance(answer, dict):
        answer = answer.get("instance_id")
    if isinstance(answer, str) and answer.strip():
        return answer.strip()
    return None


def _supports_durable_instances(journal: Any | None) -> bool:
    return journal is not None and all(
        callable(getattr(journal, name, None)) for name in _DURABLE_METHODS)


class ProviderInstanceMixin:
    """Mints provider order ids that carry the bound connection instance.

    An adapter adopts a :class:`ProviderInstanceIdentity` when the
    ``ConnectionManager`` binds it to a connection. With a journal-backed
    identity the order sequence is allocated durably, so a restarted process
    continues numbering where the last one stopped instead of repeating it.
    Without one the adapter numbers its own orders, and the instance id it
    mints at construction keeps the two processes apart regardless.
    """

    order_prefix = "ORDER"

    def _init_provider_identity(self) -> None:
        self.provider_instance_id = mint_instance_id()
        self._bound_sequence: Callable[[], int] | None = None
        self._local_sequence = 1

    def bind_provider_identity(self, identity: Any) -> None:
        """Adopt the instance id and sequence of the connection this serves."""
        instance_id = getattr(identity, "instance_id", None) or identity
        instance_id = str(instance_id or "").strip()
        if not instance_id:
            return
        self.provider_instance_id = instance_id
        sequence = getattr(identity, "next_sequence", None)
        self._bound_sequence = sequence if callable(sequence) else None

    def _next_sequence(self) -> int:
        if self._bound_sequence is not None:
            return int(self._bound_sequence())
        sequence = self._local_sequence
        self._local_sequence = sequence + 1
        return sequence

    def _mint_order_id(self) -> str:
        """``PREFIX-<instance>-<sequence>``: unique across restarts and adapters."""
        return f"{self.order_prefix}-{self.provider_instance_id}-{self._next_sequence():06d}"
