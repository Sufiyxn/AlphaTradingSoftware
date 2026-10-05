from .config import BacktestConfig


def position_size(capital: float, entry_price: float, stop_distance: float, config: BacktestConfig, *, current_exposure: float = 0.0) -> int:
    """Size whole shares under both per-trade risk and portfolio exposure limits."""
    if capital <= 0 or entry_price <= 0 or stop_distance <= 0:
        return 0
    risk_budget = capital * config.risk_fraction
    risk_limited = int(risk_budget // stop_distance)
    available_exposure = max(0.0, capital * config.max_exposure_fraction - current_exposure)
    exposure_limited = int(available_exposure // entry_price)
    return max(0, min(risk_limited, exposure_limited))
