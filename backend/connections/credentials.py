"""Out-of-band credential handling for execution connections.

Storage boundary (documented, not pretended):

* Secrets come from process configuration (``.env`` / environment) read at
  boot and are held only in this process's memory on a :class:`CredentialHandle`.
* A handle never serialises a value: ``describe()`` reports field presence,
  ``repr()``/``str()`` report counts, and ``redact()`` masks values that leak
  into provider error text.
* Encrypted-at-rest credential persistence is **not implemented in this
  phase** and no vault is simulated. When a future security phase adds
  encrypted persistence, only this module's storage boundary changes - the
  connection manager and every API response already read presence only.
"""
from __future__ import annotations

from typing import Iterable, Mapping

_REDACTED = "***"
_MIN_REDACT_LENGTH = 4


class CredentialHandle:
    """Holds one connection's secret values outside every metadata model."""

    __slots__ = ("_values", "_source")

    def __init__(self, values: Mapping[str, object] | None = None, *,
                 source: str = "process environment") -> None:
        cleaned: dict[str, str] = {}
        for name, value in dict(values or {}).items():
            if value is None:
                continue
            text = str(value).strip()
            if text:
                cleaned[str(name).strip().upper()] = text
        self._values = cleaned
        self._source = str(source)

    @property
    def source(self) -> str:
        """Where the values came from - never the values themselves."""
        return self._source

    @property
    def count(self) -> int:
        return len(self._values)

    def present(self) -> tuple[str, ...]:
        """Names of fields that hold a value (values never leave this object)."""
        return tuple(sorted(self._values))

    def missing(self, required: Iterable[str]) -> tuple[str, ...]:
        """Required field names with no value, in the order given."""
        return tuple(name for name in required if not self._values.get(name.upper()))

    def has(self, name: str) -> bool:
        return bool(self._values.get(str(name).strip().upper()))

    def get(self, name: str) -> str | None:
        """Internal adapter-facing read. Callers must never log the result."""
        return self._values.get(str(name).strip().upper())

    def describe(self, required: Iterable[str] = ()) -> list[dict]:
        """Presence-only view: field names and booleans, never values."""
        names = list(dict.fromkeys(list(required) + list(self.present())))
        return [{"name": name, "configured": self.has(name)} for name in names]

    def redact(self, text: object) -> str:
        """Mask every stored secret that appears in provider text."""
        if text is None:
            return ""
        result = str(text)
        for value in self._values.values():
            if len(value) >= _MIN_REDACT_LENGTH and value in result:
                result = result.replace(value, _REDACTED)
        return result

    def __repr__(self) -> str:
        return f"CredentialHandle(fields={len(self._values)}, source={self._source!r})"

    __str__ = __repr__
