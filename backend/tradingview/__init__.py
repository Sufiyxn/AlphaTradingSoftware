"""TradingView webhook integration - a signal source, never a price source.

TradingView alerts are authenticated with a dedicated shared secret, validated
and normalized at this boundary, deduplicated, then handed to the existing
risk and paper-trading workflow. The alert price is signal/entry metadata
only; it is never written into a position's market price.
"""
from .auth import TradingViewAuthenticator, WebhookAuthenticationError
from .payload import body_digest, extract_candidate, parse_json_body
from .service import TradingViewWebhookService, WebhookResponse

__all__ = [
    "TradingViewAuthenticator",
    "TradingViewWebhookService",
    "WebhookAuthenticationError",
    "WebhookResponse",
    "body_digest",
    "extract_candidate",
    "parse_json_body",
]
