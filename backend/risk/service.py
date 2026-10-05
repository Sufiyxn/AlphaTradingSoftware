import math

from backend.portfolio.models import PortfolioSnapshot
from backend.strategies.models import Direction, TradeSetup

from .config import RiskConfig
from .models import RiskDecision, RiskStatus
from .sizing import size_position


class RiskEngine:
    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()
        self._kill_switch = False

    @property
    def kill_switch_enabled(self) -> bool:
        return self._kill_switch

    def set_kill_switch(self, enabled: bool) -> None:
        self._kill_switch = bool(enabled)

    def evaluate(self, setup: TradeSetup, portfolio: PortfolioSnapshot) -> RiskDecision:
        reasons: list[str] = []
        if not self.config.enabled:
            reasons.append("risk engine disabled")
        if self._kill_switch:
            reasons.append("kill switch enabled")
        values = (setup.entry_price, setup.stop_price, setup.target_price)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            reasons.append("entry, stop, and target must be positive finite prices")
        if setup.direction not in (Direction.LONG, Direction.SHORT):
            reasons.append("direction must be LONG or SHORT")
        else:
            correct_stop = setup.stop_price < setup.entry_price if setup.direction is Direction.LONG else setup.stop_price > setup.entry_price
            correct_target = setup.target_price > setup.entry_price if setup.direction is Direction.LONG else setup.target_price < setup.entry_price
            if not correct_stop:
                reasons.append("stop is on the wrong side of entry")
            if not correct_target:
                reasons.append("target is on the wrong side of entry")
        risk = abs(setup.entry_price - setup.stop_price)
        reward = abs(setup.target_price - setup.entry_price)
        ratio = reward / risk if risk > 0 else 0.0
        if ratio < self.config.min_reward_risk:
            reasons.append("reward/risk is below minimum")
        if risk > portfolio.equity * self.config.max_risk_per_trade:
            reasons.append("risk per share exceeds configured per-trade risk limit")
        if risk > portfolio.available_capital * self.config.risk_fraction:
            reasons.append("risk per share exceeds available-capital risk budget")
        if portfolio.daily_pnl <= -portfolio.equity * self.config.max_daily_loss_fraction:
            reasons.append("maximum daily loss reached")
        if len(portfolio.positions) >= self.config.max_simultaneous_positions:
            reasons.append("maximum simultaneous positions reached")
        if not self.config.allow_duplicate_symbol and any(position.symbol == setup.symbol.upper() for position in portfolio.positions):
            reasons.append("duplicate symbol position prohibited")
        quantity = size_position(setup.entry_price, setup.stop_price, portfolio, self.config)
        if self.config.max_risk_amount is not None and risk * quantity > self.config.max_risk_amount:
            reasons.append("total trade risk exceeds configured maximum risk amount")
        if quantity <= 0:
            reasons.append("insufficient available capital or position capacity")
        if quantity * setup.entry_price > self.config.max_position_notional:
            reasons.append("maximum position size exceeded")
        if portfolio.exposure + quantity * setup.entry_price > portfolio.equity * self.config.max_portfolio_exposure_fraction:
            reasons.append("maximum portfolio exposure exceeded")
        status = RiskStatus.REJECTED if reasons else RiskStatus.APPROVED
        return RiskDecision(status, setup, quantity if status is RiskStatus.APPROVED else 0,
                            risk * quantity if status is RiskStatus.APPROVED else 0.0, tuple(reasons))
