"""Deterministic position sizing against the canonical portfolio snapshot.

Sizing is a constraint solver, not a forecast. It answers one question: what
is the largest whole share quantity that satisfies EVERY configured limit at
once? A result of zero means the trade cannot be placed and must be rejected.

Formula (all terms come from :class:`backend.risk.config.RiskConfig` and the
live :class:`backend.portfolio.models.PortfolioSnapshot` - never from a
fabricated price or account value)::

    distance          = |entry - stop|                     (must be > 0)
    risk_budget       = min(available_capital * risk_fraction,
                            equity * max_risk_per_trade,
                            max_risk_amount when configured)
    risk_quantity     = risk_budget // distance
    exposure_quantity = max(0, equity * max_portfolio_exposure_fraction
                                 - current_exposure) // entry
    cash_quantity     = available_capital // entry
    notional_quantity = max_position_notional // entry
    symbol_quantity   = max(0, max_symbol_exposure - same-symbol exposure) // entry
    safe_quantity     = min(risk_quantity, exposure_quantity, cash_quantity,
                            notional_quantity, symbol_quantity, max_quantity)

The caller (RiskEngine) then takes ``approved = min(safe_quantity,
requested_quantity)`` - a request is only ever reduced, never increased. This
sizing arithmetic constrains exposure; it makes no claim about profitability.
"""
from backend.portfolio.models import PortfolioSnapshot
from backend.strategies.models import Direction

from .config import RiskConfig


def sizing_terms(entry: float, stop: float, portfolio: PortfolioSnapshot,
                 config: RiskConfig, *, symbol: str | None = None,
                 direction: Direction | None = None) -> dict[str, int]:
    """Each capacity constraint as a share quantity; sizing is their minimum.

    The mapping keeps insertion order (risk, exposure, cash, notional,
    symbol, max quantity) so callers can name every binding constraint
    deterministically when the result is zero.
    """
    distance = abs(entry - stop)
    if distance <= 0 or entry <= 0:
        return {"risk_budget": 0, "portfolio_exposure": 0, "cash": 0,
                "position_notional": 0, "max_quantity": int(config.max_quantity)}
    risk_budget = min(portfolio.available_capital * config.risk_fraction,
                      portfolio.equity * config.max_risk_per_trade)
    if config.max_risk_amount is not None:
        risk_budget = min(risk_budget, config.max_risk_amount)
    exposure_room = max(0.0, portfolio.equity * config.max_portfolio_exposure_fraction
                        - portfolio.exposure)
    cash = max(0.0, portfolio.available_capital)
    terms = {
        "risk_budget": int(risk_budget // distance),
        "portfolio_exposure": int(exposure_room // entry),
        "cash": int(cash // entry),
        "position_notional": int(config.max_position_notional // entry),
        "max_quantity": int(config.max_quantity),
    }
    if symbol is not None:
        wanted = symbol.strip().upper()
        held = sum(position.quantity * (
            position.average_entry_price if position.market_price is None else position.market_price)
            for position in portfolio.positions
            if position.symbol == wanted and (direction is None or position.direction is direction))
        symbol_room = max(0.0, config.max_symbol_exposure - held)
        terms["symbol_exposure"] = int(symbol_room // entry)
    return terms


def size_position(entry: float, stop: float, portfolio: PortfolioSnapshot,
                  config: RiskConfig, *, symbol: str | None = None,
                  direction: Direction | None = None) -> int:
    terms = sizing_terms(entry, stop, portfolio, config,
                         symbol=symbol, direction=direction)
    return max(0, min(terms.values()))
