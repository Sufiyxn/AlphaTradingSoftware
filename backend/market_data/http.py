"""Stdlib HTTP transport behind an injectable interface.

Every vendor adapter reaches the network only through :class:`HttpTransport`,
so tests can substitute deterministic responses and CI never contacts a real
API. Nothing in this module logs, stores, or returns a URL that carries a
credential: transport failures report the failure itself, never the request.
"""
from dataclasses import dataclass
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .provider import ProviderConnectionError, ProviderTimeoutError


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One HTTP response. Header keys are lower-cased."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str] | None = None,
            timeout: float) -> HttpResponse:
        """Perform one GET and return the response, whatever its status."""
        ...


def with_params(url: str, params: Mapping[str, object] | None) -> str:
    """Attach query parameters to a URL without disturbing an existing query."""
    if not params:
        return url
    parts = urlsplit(url)
    query = list(parse_qsl(parts.query, keep_blank_values=True))
    query.extend((key, str(value)) for key, value in params.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def without_query(url: str) -> str:
    """Strip the query string so a URL can never leak a credential in an error."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class UrllibTransport:
    """Production transport built on the standard library only."""

    def get(self, url: str, *, headers: Mapping[str, str] | None = None,
            timeout: float) -> HttpResponse:
        request = Request(url, headers=dict(headers or {}), method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(response.status,
                                    {key.lower(): value
                                     for key, value in response.headers.items()},
                                    response.read())
        except HTTPError as error:
            body = b""
            try:
                body = error.read()
            except Exception:
                pass
            return HttpResponse(error.code,
                                {key.lower(): value
                                 for key, value in (error.headers or {}).items()},
                                body)
        except TimeoutError:
            raise ProviderTimeoutError(
                f"the provider request timed out after {timeout:g} seconds") from None
        except URLError as error:
            reason = error.reason
            if isinstance(reason, TimeoutError):
                raise ProviderTimeoutError(
                    f"the provider request timed out after {timeout:g} seconds") from None
            raise ProviderConnectionError(
                "the provider could not be reached; no response was received") from None
        except OSError:
            raise ProviderConnectionError(
                "the provider could not be reached; no response was received") from None
