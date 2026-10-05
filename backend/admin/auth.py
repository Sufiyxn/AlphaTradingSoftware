import hmac
import os
from typing import Protocol


class OwnerAuthenticator(Protocol):
    def authenticate(self, credential: str | None) -> str | None: ...


class LocalOwnerAuthenticator:
    """Local authentication seam; token is supplied by the environment or caller."""

    def __init__(self, token: str, owner_name: str = "owner"):
        if not token:
            raise ValueError("owner control token must not be empty")
        self._token = token
        self.owner_name = owner_name

    @classmethod
    def from_environment(cls, variable: str = "OWNER_CONTROL_TOKEN") -> "LocalOwnerAuthenticator":
        token = os.environ.get(variable)
        if not token:
            raise RuntimeError(f"{variable} must be set to enable owner controls")
        return cls(token)

    def authenticate(self, credential: str | None) -> str | None:
        if credential is None or not hmac.compare_digest(self._token, credential):
            return None
        return self.owner_name
