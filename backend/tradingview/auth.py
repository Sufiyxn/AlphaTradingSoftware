"""TradingView webhook authentication.

Two mutually supported, dedicated mechanisms - both keyed by
``TRADINGVIEW_WEBHOOK_SECRET`` alone, never by the owner token:

1. ``X-Webhook-Signature: sha256=<hex>`` - HMAC-SHA256 of the exact request
   body, for anything that can sign before posting.
2. A shared secret field in the JSON body (``secret``), for TradingView alert
   messages, which cannot compute an HMAC.

Every comparison is constant time, failures carry no detail that would echo a
credential, and the secret is never logged, journaled, or returned.
"""
import hashlib
import hmac
from typing import Any, Mapping

SIGNATURE_HEADERS = (
    "x-webhook-signature",
    "x-tradingview-signature",
    "x-signature",
)
PAYLOAD_SECRET_FIELDS = ("secret", "webhook_secret", "token")


class WebhookAuthenticationError(Exception):
    """Missing or invalid webhook authentication."""

    reason = "AUTH_FAILED"

    def __init__(self, message: str = "webhook authentication failed"):
        super().__init__(message)
        self.message = message


def _equal(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except (UnicodeEncodeError, AttributeError):
        return False


class TradingViewAuthenticator:
    """Verifies one configured shared secret against a request."""

    def __init__(self, secret: str):
        if not secret or not str(secret).strip():
            raise ValueError("webhook secret must not be empty")
        self._secret = str(secret)

    @property
    def configured(self) -> bool:
        return bool(self._secret)

    def signature(self, body: bytes) -> str:
        """HMAC-SHA256 hex digest of the exact request body."""
        digest = hmac.new(self._secret.encode("utf-8"), body or b"", hashlib.sha256)
        return digest.hexdigest()

    def header_names(self, headers: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(name for name in SIGNATURE_HEADERS if self._header_value(headers, name))

    @staticmethod
    def _header_value(headers: Mapping[str, Any], name: str) -> str | None:
        items = headers.items() if hasattr(headers, "items") else ()
        for key, value in items:
            if str(key).lower() == name and value is not None and str(value).strip():
                return str(value).strip()
        return None

    def verify_header(self, body: bytes, headers: Mapping[str, Any]) -> None:
        """Verify a signature header; raises when present but wrong."""
        provided = None
        for name in SIGNATURE_HEADERS:
            found = self._header_value(headers, name)
            if found:
                provided = found
                break
        if not provided:
            raise WebhookAuthenticationError("no webhook signature was supplied")
        candidate = provided.split("=", 1)[1] if "=" in provided else provided
        if not _equal(self.signature(body), candidate.strip().lower()):
            raise WebhookAuthenticationError("webhook signature did not verify")

    def verify_payload(self, payload: Mapping[str, Any]) -> None:
        """Verify a shared-secret field carried in the JSON body."""
        if not isinstance(payload, Mapping):
            raise WebhookAuthenticationError("webhook authentication could not be verified")
        supplied = None
        for field in PAYLOAD_SECRET_FIELDS:
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                supplied = value
                break
        if supplied is None:
            raise WebhookAuthenticationError("no webhook credential was supplied")
        if not _equal(self._secret, supplied):
            raise WebhookAuthenticationError("webhook credential did not verify")

    def verify(self, body: bytes, headers: Mapping[str, Any],
               payload: Mapping[str, Any] | None = None) -> None:
        """Verify whichever credential the request carries, failing closed."""
        if self.header_names(headers):
            self.verify_header(body, headers)
            return
        if payload is None:
            raise WebhookAuthenticationError("no webhook credential was supplied")
        self.verify_payload(payload)
