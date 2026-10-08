"""Durable identity for provider fills.

A fill that enters the order lifecycle gets exactly one identity before it is
merged, journaled, or compared - and that identity survives serialization, a
process restart, and any number of replays of the same provider answer.

Two levels, in order of preference:

* when the provider supplied ``provider_fill_id``, that number *is* the fill's
  identity - the platform never invents a competing one to replace it;
* when it did not, the identity is derived deterministically from
  ``client_order_id + provider_order_id + quantity + price + timestamp``
  through a documented canonical serialization, so the same execution always
  derives the same identity. No random identifier is ever used as a fallback.

Both forms are qualified by the provider scope they were seen in -
``connection_id + provider_instance_id``, the same scope phase 10 CP1 gave
provider *order* identities - because a number a provider hands out is only
unique for the connection that saw it. The same ``provider_fill_id`` observed
on two scopes is two different fills; the same fill observed twice in one
scope is one fill.

Consequence of the deterministic fallback, stated plainly: two executions that
are identical in every identifying field - same order, same quantity, same
price, same instant - are one fill as far as the platform can tell, because
without a provider number nothing can tell them apart. A provider that wants
two same-moment partials kept apart must identify them; the paper venue does
(``<provider_order_id>-F<n>``), and any provider number is adopted verbatim.

Nothing here talks to a journal, a provider, or a clock. It answers one
question: "given this execution and the order it belongs to, what is its
durable identity?"
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone

from .models import Fill

__all__ = ["deterministic_fill_key", "fill_identity", "normalize_fill"]

# ASCII unit separator: never appears in the text we join, so no combination
# of field values can forge a boundary and collide with a different payload.
_FIELD_SEPARATOR = "\x1f"
# The scope a fill was seen in when the caller could not name it.
_SCOPE_PLACEHOLDER = "-"


def deterministic_fill_key(*, quantity: float, price: float,
                           timestamp: datetime,
                           client_order_id: str | None = None,
                           provider_order_id: str | None = None) -> str:
    """The deterministic identity of a fill no provider identified.

    The five identifying fields are serialized in a fixed order and joined
    with the unit separator:

    1. ``client_order_id``   - stripped text, empty when absent;
    2. ``provider_order_id`` - stripped text, empty when absent;
    3. ``quantity``          - ``repr(float(...))``, Python's shortest
                               round-trip form, so ``100``, ``100.0`` and
                               ``100.00`` all serialize as ``100.0``;
    4. ``price``             - the same form as quantity;
    5. ``timestamp``         - ISO-8601 of the instant, normalized to UTC
                               first when it carries a time zone (a naive
                               timestamp is taken to already be UTC).

    The SHA-256 of those bytes is the key, prefixed ``FILL-`` so a key is
    never mistaken for a provider number. Equal inputs - after any JSON
    round-trip - produce an equal key; different inputs never share one.
    """
    payload = _FIELD_SEPARATOR.join((
        _text(client_order_id),
        _text(provider_order_id),
        _number(quantity),
        _number(price),
        _timestamp(timestamp),
    ))
    return f"FILL-{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def fill_identity(*, quantity: float, price: float, timestamp: datetime,
                  connection_id: str | None = None,
                  provider_instance_id: str | None = None,
                  provider_fill_id: str | None = None,
                  client_order_id: str | None = None,
                  provider_order_id: str | None = None) -> str:
    """The durable, scope-qualified identity of one provider fill.

    ``<connection_id>:<provider_instance_id>:<key>`` where ``key`` is the
    provider's own fill number when it has one (stripped, never rewritten)
    and :func:`deterministic_fill_key` when it does not. Scope parts that
    are missing render as ``-`` rather than an empty segment, so the shape
    of an identity never depends on how much of the scope was known.
    """
    key = _text(provider_fill_id) or deterministic_fill_key(
        quantity=quantity, price=price, timestamp=timestamp,
        client_order_id=client_order_id, provider_order_id=provider_order_id)
    return f"{_scope_part(connection_id)}:{_scope_part(provider_instance_id)}:{key}"


def normalize_fill(fill: Fill, *, connection_id: str | None = None,
                   provider_instance_id: str | None = None,
                   client_order_id: str | None = None,
                   provider_order_id: str | None = None) -> Fill:
    """Give ``fill`` its durable identity and order attribution.

    Idempotent by construction: a fill that already carries a ``fill_id``
    is returned untouched, so an identity, once assigned, is never
    re-derived or rewritten - replaying or re-scoping a recorded fill
    cannot silently mint a second identity for it. A fill without one is
    identified from its own execution fields (preferring the provider's
    fill number) and scoped by the connection and instance that saw it,
    and its order attribution is back-filled only where it is missing.
    """
    if fill.fill_id:
        return fill
    owner = client_order_id if client_order_id is not None else fill.order_id
    provider = (provider_order_id if provider_order_id is not None
                else fill.provider_order_id)
    identity = fill_identity(
        connection_id=connection_id,
        provider_instance_id=provider_instance_id,
        provider_fill_id=fill.provider_fill_id,
        client_order_id=owner,
        provider_order_id=provider,
        quantity=fill.quantity,
        price=fill.price,
        timestamp=fill.timestamp,
    )
    return replace(fill,
                   fill_id=identity,
                   order_id=fill.order_id or owner,
                   provider_order_id=fill.provider_order_id or provider)


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _scope_part(value: object) -> str:
    return _text(value) or _SCOPE_PLACEHOLDER


def _number(value: object) -> str:
    try:
        return repr(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _text(value)


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.isoformat()
    return _text(value)
