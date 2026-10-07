"""Server settings entry point.

The implementation lives in :mod:`backend.config`; this module keeps the
historical import path used by the HTTP layer and existing tests.
"""
from backend.config import Settings, load_config, load_local_env, load_settings

__all__ = ["Settings", "load_config", "load_local_env", "load_settings"]
