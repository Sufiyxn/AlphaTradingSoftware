import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    environment: str = "development"
    database_path: str = "data/alpha_trading.sqlite3"
    owner_token: str | None = None


def load_settings() -> Settings:
    """Load the small set of non-secret application settings from the environment."""
    _load_local_env()
    return Settings(
        host=os.environ.get("APP_HOST", "127.0.0.1"),
        port=int(os.environ.get("APP_PORT", "8000")),
        environment=os.environ.get("APP_ENV", "development"),
        database_path=os.environ.get("DATABASE_PATH", "data/alpha_trading.sqlite3"),
        owner_token=os.environ.get("OWNER_CONTROL_TOKEN") or None,
    )


def _load_local_env() -> None:
    """Read simple KEY=value entries without overriding the process environment."""
    path = Path(".env")
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        key, value = key.strip(), value.strip().strip("\"'")
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)
