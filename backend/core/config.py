import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    environment: str = "development"


def load_settings() -> Settings:
    """Load the small set of non-secret application settings from the environment."""
    return Settings(
        host=os.environ.get("APP_HOST", "127.0.0.1"),
        port=int(os.environ.get("APP_PORT", "8000")),
        environment=os.environ.get("APP_ENV", "development"),
    )
