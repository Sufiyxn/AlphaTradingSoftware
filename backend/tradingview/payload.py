"""TradingView alert payload handling - the integration boundary.

Everything TradingView-specific lives here: JSON decoding with a hard size
limit, alert field aliasing, the alert action vocabulary, and deterministic
event identity. Downstream code only ever sees ``NormalizedSignal``; it never
reads a TradingView field name, and no alert value is treated as a market
price.
"""
import hashlib
import json
from typing import Any, Mapping

from backend.signals import SignalValidationError

ACTION_ALIASES: dict[str, str] = {
    "buy": "ENTER_LONG",
    "long": "ENTER_LONG",
    "go_long": "ENTER_LONG",
    "entry_long": "ENTER_LONG",
    "buy_to_open": "ENTER_LONG",
    "short": "ENTER_SHORT",
    "sell_short": "ENTER_SHORT",
    "go_short": "ENTER_SHORT",
    "entry_short": "ENTER_SHORT",
    "short_sell": "ENTER_SHORT",
    "sell": "EXIT",
    "sell_to_close": "EXIT",
    "close": "EXIT",
    "close_position": "EXIT",
    "exit": "EXIT",
    "flat": "EXIT",
    "cover": "EXIT",
    "close_all": "EXIT",
}

SUPPORTED_ACTIONS = ("ENTER_LONG", "ENTER_SHORT", "EXIT")

_ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "ticker", "instrument", "tradingsymbol"),
    "action": ("action", "side", "signal_action", "order_action", "alert_action", "signal"),
    "quantity": ("quantity", "qty", "size", "shares", "contracts"),
    "entry": ("entry_price", "entry", "open_price", "price", "alert_price"),
    "stop": ("stop_price", "stop", "stop_loss", "stoploss", "sl"),
    "target": ("target_price", "target", "take_profit", "takeprofit", "tp"),
    "reported": ("price", "exit_price", "alert_price", "close", "last"),
    "timestamp": ("timestamp", "time", "datetime", "alert_time", "ts", "created_at"),
    "strategy": ("strategy", "strategy_name", "alert_name", "alert", "study"),
    "signal_id": ("signal_id", "alert_id", "event_id", "id", "alertid", "client_id"),
}


def parse_json_body(raw: bytes) -> dict[str, Any]:
    """Decode a request body into a JSON object, rejecting anything else."""
    if not raw or not raw.strip():
        raise SignalValidationError("request body must not be empty",
                                    reason="EMPTY_BODY")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SignalValidationError("request body must be UTF-8 encoded",
                                    reason="MALFORMED_JSON") from None
    try:
        payload = json.loads(text)
    except ValueError:
        raise SignalValidationError("request body is not valid JSON",
                                    reason="MALFORMED_JSON") from None
    if not isinstance(payload, dict):
        raise SignalValidationError("request body must be a JSON object",
                                    reason="INVALID_PAYLOAD")
    return payload


def body_digest(raw: bytes) -> str:
    """Deterministic identity of the exact bytes that were delivered."""
    return hashlib.sha256(raw or b"").hexdigest()


def _first(payload: Mapping[str, Any], field: str) -> Any:
    for name in _ALIASES[field]:
        if name in payload and payload[name] is not None:
            return payload[name]
    return None


def _has(payload: Mapping[str, Any], field: str) -> bool:
    return any(name in payload and payload[name] is not None for name in _ALIASES[field])


def extract_candidate(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Map a TradingView alert onto the canonical signal field names."""
    candidate: dict[str, Any] = {}

    if _has(payload, "symbol"):
        candidate["symbol"] = _first(payload, "symbol")

    if _has(payload, "action"):
        raw_action = _first(payload, "action")
        if isinstance(raw_action, str):
            key = raw_action.strip().lower().replace("-", "_").replace(" ", "_")
            mapped = ACTION_ALIASES.get(key)
            if mapped is None:
                raise SignalValidationError(
                    f"unsupported action '{raw_action.strip()}'; supported actions: "
                    f"{', '.join(SUPPORTED_ACTIONS)}", reason="UNSUPPORTED_ACTION")
            candidate["action"] = mapped
        else:
            candidate["action"] = raw_action

    if _has(payload, "quantity"):
        candidate["quantity"] = _first(payload, "quantity")
    if _has(payload, "entry"):
        candidate["entry_price"] = _first(payload, "entry")
    if _has(payload, "stop"):
        candidate["stop_price"] = _first(payload, "stop")
    if _has(payload, "target"):
        candidate["target_price"] = _first(payload, "target")
    if _has(payload, "reported"):
        candidate["reported_price"] = _first(payload, "reported")
    if _has(payload, "timestamp"):
        candidate["timestamp"] = _first(payload, "timestamp")
    if _has(payload, "strategy"):
        candidate["strategy"] = _first(payload, "strategy")

    signal_id = _first(payload, "signal_id")
    if signal_id is not None:
        candidate["explicit_id"] = signal_id

    candidate["metadata"] = _metadata(payload)
    return candidate


def explicit_id(candidate: Mapping[str, Any]) -> str | None:
    """Validate and return a caller-supplied event id, if any."""
    value = candidate.get("explicit_id")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SignalValidationError("event id must be a string or number",
                                    reason="INVALID_PAYLOAD")
    text = str(value).strip()
    if not text or len(text) > 128:
        raise SignalValidationError("event id must be 1-128 characters",
                                    reason="INVALID_PAYLOAD")
    return text


def _metadata(payload: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Carry a few safe provider hints across the boundary as strings."""
    allowed = ("exchange", "interval", "plot", "close", "volume", "currency")
    pairs = []
    for name in allowed:
        value = payload.get(name)
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            pairs.append((name, str(value)[:256]))
    return tuple(pairs)
