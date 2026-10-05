from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class SymbolUniverse:
    symbols: tuple[str, ...]

    def __init__(self, symbols: Iterable[str]):
        normalized = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols if isinstance(symbol, str) and symbol.strip()))
        object.__setattr__(self, "symbols", normalized)
