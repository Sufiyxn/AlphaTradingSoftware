import math
from datetime import datetime, timezone

from backend.portfolio.models import PortfolioSnapshot
from backend.strategies.models import Direction, TradeSetup

from .config import RiskConfig
from .models import RiskCode, RiskDecision, RiskStatus
from .sizing import sizing_terms


def _finite_positive(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


#: Which rule and message names each sizing constraint when it binds to zero.
#: Keys are the SHARE-COUNT keys of :func:`backend.risk.sizing.sizing_terms`.
_BINDING_LIMIT_CODES = {
    "risk_quantity": RiskCode.TRADE_RISK_EXCEEDED,
    "portfolio_exposure": RiskCode.PORTFOLIO_EXPOSURE_EXCEEDED,
    "cash": RiskCode.ZERO_SAFE_QUANTITY,
    "position_notional": RiskCode.POSITION_NOTIONAL_EXCEEDED,
    "symbol_exposure": RiskCode.SYMBOL_EXPOSURE_EXCEEDED,
}

_BINDING_LIMIT_REASONS = {
    "risk_quantity": "position sizing found no room inside the risk budget",
    "portfolio_exposure": "no capacity remains under the portfolio exposure limit",
    "cash": "no available cash capacity for this position",
    "position_notional": "the entry price exceeds the maximum position notional",
    "symbol_exposure": "no capacity remains under the symbol exposure limit",
}


class RiskEngine:
    """The single canonical risk decision point for every production order.

    Evaluation order is fixed and deterministic; ``RiskDecision.code`` names
    the FIRST failing rule and ``reasons`` lists every failing rule in the
    same order:

    1. configuration (``enabled``) and the kill switch
    2. price sanity: positive, finite entry/stop/target; entry price ceiling
    3. direction and geometry - long: stop < entry < target;
       short: target < entry < stop
    4. trade risk per share > 0, then reward:risk >= configured minimum
       (default 2.0; long reward = target - entry, risk = entry - stop;
       short reward = entry - target, risk = stop - entry)
    5. portfolio state: valuation freshness (when configured), positive
       equity, daily loss limit, simultaneous positions, duplicate symbol
    6. sizing: safe quantity from the risk budget / exposure / cash /
       notional / symbol exposure / max quantity, then
       approved = min(safe, requested) - a request is only ever reduced
    7. final invariant checks against the resulting quantity: total trade
       risk, max quantity, position notional, projected portfolio exposure

    Submitted-but-unfilled orders are carried on the snapshot as
    ``pending_reservations`` and are honoured by steps 5, 6 and 7: they hold a
    position slot, they close the duplicate-symbol door, and their remaining
    notional is subtracted from exposure room, cash and the symbol's room.
    Because only the *unfilled* remainder is reserved, a partial fill never
    counts twice - the filled part is already a position.

    Nothing here fabricates a price, an account balance, or P&L: every number
    comes from the caller's trade setup or the canonical portfolio snapshot.
    When a required value is invalid, the trade is rejected - a missing price
    is never invented. Risk evaluation is deterministic; no model or heuristic
    is consulted.
    """

    def __init__(self, config: RiskConfig | None = None, *, clock=None):
        self.config = config or RiskConfig()
        self._kill_switch = False
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def kill_switch_enabled(self) -> bool:
        return self._kill_switch

    def set_kill_switch(self, enabled: bool) -> None:
        self._kill_switch = bool(enabled)

    def evaluate(self, setup: TradeSetup, portfolio: PortfolioSnapshot,
                 *, requested_quantity: int | float | None = None) -> RiskDecision:
        reasons: list[str] = []
        codes: list[str] = []

        def reject(code: str, reason: str) -> None:
            codes.append(code)
            reasons.append(reason)

        config = self.config

        # 1. configuration and the single kill switch --------------------------
        if not config.enabled:
            reject(RiskCode.RISK_DISABLED, "risk engine disabled")
        if self._kill_switch:
            reject(RiskCode.KILL_SWITCH, "kill switch enabled")

        # 2. price sanity ------------------------------------------------------
        valid_setup = isinstance(setup, TradeSetup)
        entry = setup.entry_price if valid_setup else None
        stop = setup.stop_price if valid_setup else None
        target = setup.target_price if valid_setup else None
        prices_valid = valid_setup and all(
            _finite_positive(value) for value in (entry, stop, target))
        if not prices_valid:
            reject(RiskCode.INVALID_PRICE,
                   "entry, stop, and target must be positive finite prices")
        elif entry > config.max_entry_price:
            reject(RiskCode.ENTRY_PRICE_LIMIT,
                   f"entry price {entry:g} exceeds the configured maximum "
                   f"{config.max_entry_price:g}")

        # 3. direction and stop/target geometry --------------------------------
        direction = setup.direction if valid_setup else None
        stop_ok = target_ok = False
        if direction not in (Direction.LONG, Direction.SHORT):
            reject(RiskCode.INVALID_DIRECTION, "direction must be LONG or SHORT")
        elif prices_valid:
            if direction is Direction.LONG:
                stop_ok = stop < entry
                target_ok = target > entry
            else:
                stop_ok = stop > entry
                target_ok = target < entry
            if not stop_ok:
                reject(RiskCode.STOP_SIDE_INVALID, "stop is on the wrong side of entry")
            if not target_ok:
                reject(RiskCode.TARGET_SIDE_INVALID, "target is on the wrong side of entry")

        # signed risk/reward per share for the intended direction ---------------
        if direction is Direction.LONG and prices_valid:
            risk_per_unit = float(entry - stop)
            reward_per_unit = float(target - entry)
        elif direction is Direction.SHORT and prices_valid:
            risk_per_unit = float(stop - entry)
            reward_per_unit = float(entry - target)
        else:
            risk_per_unit = reward_per_unit = 0.0

        # 4. trade risk and reward:risk ------------------------------------------
        ratio = (reward_per_unit / risk_per_unit
                 if risk_per_unit > 0 and math.isfinite(risk_per_unit)
                 and math.isfinite(reward_per_unit) else 0.0)
        if prices_valid and direction in (Direction.LONG, Direction.SHORT):
            if risk_per_unit <= 0 or not math.isfinite(risk_per_unit):
                reject(RiskCode.ZERO_RISK, "trade risk per share must be positive")
            elif reward_per_unit <= 0 or not math.isfinite(reward_per_unit) \
                    or ratio < config.min_reward_risk:
                reject(RiskCode.REWARD_RISK_TOO_LOW, "reward/risk is below minimum")

        # 5. canonical portfolio state -------------------------------------------
        equity = float(portfolio.equity)
        exposure = float(portfolio.exposure)
        daily_pnl = float(portfolio.daily_pnl)
        pending = tuple(getattr(portfolio, "pending_reservations", ()) or ())
        pending_notional = sum(item.notional for item in pending)
        pending_symbols = {item.symbol for item in pending}
        held_symbols = {position.symbol for position in portfolio.positions}
        reserved_slots = len(pending_symbols - held_symbols)
        symbol = str(setup.symbol).strip().upper() if valid_setup else ""
        symbol_open = bool(symbol) and (symbol in held_symbols or symbol in pending_symbols)

        if config.require_fresh_valuation:
            unfresh = [position.symbol for position in portfolio.positions
                       if position.price_state.value != "FRESH"]
            if unfresh:
                reject(RiskCode.VALUATION_NOT_FRESH,
                       "portfolio valuation is not fresh: at least one open "
                       "position has no fresh real quote")
        if equity <= 0:
            reject(RiskCode.INVALID_EQUITY, "portfolio equity is not positive")
        elif daily_pnl <= -equity * config.max_daily_loss_fraction:
            reject(RiskCode.DAILY_LOSS_LIMIT, "maximum daily loss reached")
        # A submitted-but-unfilled order counts toward the position slot it
        # will occupy the moment it fills; a symbol that already holds a
        # position does not consume a second slot for its own pending order.
        if len(portfolio.positions) + reserved_slots >= config.max_simultaneous_positions:
            reject(RiskCode.MAX_POSITIONS_REACHED, "maximum simultaneous positions reached")
        if not config.allow_duplicate_symbol and symbol_open:
            reject(RiskCode.DUPLICATE_SYMBOL, "duplicate symbol position prohibited")

        # 6. sizing: safe quantity, then approved = min(safe, requested) ---------
        prices_and_direction_ok = prices_valid and direction in (Direction.LONG, Direction.SHORT)
        terms: dict[str, int] = {}
        if prices_and_direction_ok and risk_per_unit > 0:
            terms = sizing_terms(float(entry), float(stop), portfolio, config,
                                 symbol=symbol, direction=direction)
        safe_quantity = max(0, min(terms.values())) if terms else 0
        requested: int | float | None = requested_quantity
        if requested_quantity is not None:
            if not _finite_positive(requested_quantity):
                reject(RiskCode.INVALID_REQUEST,
                       "the requested quantity must be a positive finite number")
                requested = None
        quantity: int | float = min(safe_quantity, requested) \
            if requested is not None else safe_quantity
        if prices_and_direction_ok and safe_quantity <= 0:
            reject(RiskCode.ZERO_SAFE_QUANTITY,
                   "insufficient available capital or position capacity")
            # Name every constraint that has no room left, so the rejected
            # rule is explainable from the journal alone.
            for term in ("risk_quantity", "portfolio_exposure", "cash",
                         "position_notional", "symbol_exposure"):
                if terms.get(term, 1) <= 0:
                    reject(_BINDING_LIMIT_CODES[term], _BINDING_LIMIT_REASONS[term])

        trade_risk = risk_per_unit * quantity if quantity > 0 else 0.0
        notional = float(entry) * quantity if quantity > 0 and prices_valid else 0.0
        projected = exposure + pending_notional + notional
        symbol_held = sum(
            position.quantity * (
                position.average_entry_price if position.market_price is None else position.market_price)
            for position in portfolio.positions if symbol and position.symbol == symbol)
        pending_symbol_notional = sum(
            item.notional for item in pending if symbol and item.symbol == symbol)

        # 7. final invariant checks against the resulting quantity ----------------
        if config.max_risk_amount is not None and trade_risk > config.max_risk_amount:
            reject(RiskCode.TRADE_RISK_EXCEEDED,
                   "total trade risk exceeds configured maximum risk amount")
        if trade_risk > equity * config.max_risk_per_trade:
            reject(RiskCode.TRADE_RISK_EXCEEDED,
                   "total trade risk exceeds the per-trade risk budget")
        if trade_risk > portfolio.available_capital * config.risk_fraction:
            reject(RiskCode.TRADE_RISK_EXCEEDED,
                   "total trade risk exceeds the available-capital risk budget")
        if quantity > config.max_quantity:
            reject(RiskCode.MAX_QUANTITY_EXCEEDED,
                   f"approved quantity exceeds the configured maximum of "
                   f"{config.max_quantity} shares")
        if notional > config.max_position_notional:
            reject(RiskCode.POSITION_NOTIONAL_EXCEEDED, "maximum position size exceeded")
        if symbol_held + pending_symbol_notional + notional > config.max_symbol_exposure:
            reject(RiskCode.SYMBOL_EXPOSURE_EXCEEDED,
                   "symbol exposure would exceed the configured maximum")
        if projected > equity * config.max_portfolio_exposure_fraction:
            reject(RiskCode.PORTFOLIO_EXPOSURE_EXCEEDED,
                   "maximum portfolio exposure exceeded")

        # legacy per-share guards retained as defence in depth ---------------------
        if prices_and_direction_ok and risk_per_unit > 0:
            if risk_per_unit > equity * config.max_risk_per_trade:
                reject(RiskCode.TRADE_RISK_EXCEEDED,
                       "risk per share exceeds configured per-trade risk limit")
            if risk_per_unit > portfolio.available_capital * config.risk_fraction:
                reject(RiskCode.TRADE_RISK_EXCEEDED,
                       "risk per share exceeds available-capital risk budget")

        approved = not reasons
        return RiskDecision(
            status=RiskStatus.APPROVED if approved else RiskStatus.REJECTED,
            setup=setup,
            quantity=quantity if approved else 0,
            risk_amount=trade_risk if approved else 0.0,
            reasons=tuple(reasons),
            code=codes[0] if codes else RiskCode.APPROVED,
            requested_quantity=requested_quantity,
            entry_price=float(entry) if prices_valid else 0.0,
            stop_price=float(stop) if prices_valid else 0.0,
            target_price=float(target) if prices_valid else 0.0,
            risk_per_unit=risk_per_unit,
            reward_per_unit=reward_per_unit,
            reward_risk=ratio,
            current_exposure=exposure,
            projected_exposure=projected,
            daily_pnl=daily_pnl,
            equity=equity,
            symbol_open=symbol_open,
            pending_exposure=pending_notional,
            limits=config.limits,
            decided_at=self._clock())
