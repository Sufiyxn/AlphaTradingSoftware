import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any


def to_json(value: Any) -> str:
    return json.dumps(value, default=_encode, sort_keys=True, separators=(",", ":"))


def from_json(value: str | None) -> Any:
    return json.loads(value) if value else {}


def _encode(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, timedelta):
        # Audit context can carry an interval (a runtime scan-interval change,
        # for example). Losing the whole journal entry over an unencodable
        # field would silently break the audit trail, so the interval is
        # recorded as its exact length in seconds.
        return value.total_seconds()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")
