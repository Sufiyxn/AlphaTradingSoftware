from backend.portfolio.models import PortfolioSnapshot

from .config import RiskConfig


def size_position(entry: float, stop: float, portfolio: PortfolioSnapshot, config: RiskConfig) -> int:
    distance = abs(entry - stop)
    if distance <= 0 or entry <= 0:
        return 0
    risk_budget = min(portfolio.available_capital * config.risk_fraction,
                      portfolio.equity * config.max_risk_per_trade)
    if config.max_risk_amount is not None:
        risk_budget = min(risk_budget, config.max_risk_amount)
    risk_quantity = int(risk_budget // distance)
    exposure_room = max(0.0, portfolio.equity * config.max_portfolio_exposure_fraction - portfolio.exposure)
    exposure_quantity = int(exposure_room // entry)
    cash_quantity = int(portfolio.available_capital // entry)
    notional_quantity = int(config.max_position_notional // entry)
    return max(0, min(risk_quantity, exposure_quantity, cash_quantity, notional_quantity))
