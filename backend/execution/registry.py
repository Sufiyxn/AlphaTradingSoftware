from .interface import TradingPlatformAdapter


class AdapterRegistry:
    """Named adapter registry; the trading engine depends only on its router."""

    def __init__(self):
        self._adapters: dict[str, TradingPlatformAdapter] = {}

    def register(self, adapter: TradingPlatformAdapter, name: str | None = None) -> None:
        key = (name or adapter.identify()).strip().lower()
        if not key:
            raise ValueError("adapter name must not be empty")
        if key in self._adapters:
            raise ValueError(f"adapter already registered: {key}")
        self._adapters[key] = adapter

    def get(self, name: str) -> TradingPlatformAdapter:
        try:
            return self._adapters[name.strip().lower()]
        except KeyError as error:
            raise KeyError(f"unknown trading adapter: {name}") from error

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))
