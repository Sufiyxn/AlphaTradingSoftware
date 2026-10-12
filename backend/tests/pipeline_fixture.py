"""Open a paper position through the canonical pipeline - no signal vendor.

Tests that used to prove a workflow by posting a TradingView alert now submit
the same trade setup straight to the paper engine, which runs the very same
TradingPipeline (risk evaluation, session gate, safety gate, paper adapter)
that every other signal path travels. What the tests assert about exits, cash,
journals and restarts is unchanged.

Every price here is an explicit number the test chose; nothing is derived,
marked or invented, and no vendor is contacted.
"""
from __future__ import annotations

from datetime import datetime

from backend.strategies.models import Direction, TradeSetup


def trade_setup(*, timestamp: datetime, symbol: str = "AAPL",
                entry: float = 50.0, stop: float = 48.0, target: float = 54.0,
                strategy_name: str = "pipeline-fixture") -> TradeSetup:
    """A long setup with an explicit 2:1 reward/risk, stamped at ``timestamp``."""
    risk = entry - stop
    return TradeSetup(
        symbol=symbol, direction=Direction.LONG, entry_price=entry,
        stop_price=stop, target_price=target, risk_per_share=risk,
        reward_per_share=target - entry, reward_risk_ratio=(target - entry) / risk,
        timestamp=timestamp, strategy_name=strategy_name, score=1.0,
        explanation="explicit fixture setup submitted through the canonical pipeline")


def submit(runtime, setup: TradeSetup, *, quantity: int | float | None = None,
           source: str = "test"):
    """Hand one setup to the paper engine through the canonical pipeline."""
    return runtime.paper.submit(setup, source=source, requested_quantity=quantity)


def open_position(runtime, *, symbol: str = "AAPL",
                  quantity: int | float | None = 5) -> dict:
    """Open one paper position and fail loudly if the pipeline refused it."""
    result = submit(runtime, trade_setup(symbol=symbol, timestamp=runtime.clock()),
                    quantity=quantity)
    if not result.accepted or result.trade is None:
        detail = f"{result.code}: {result.reason}" if result.code else "no trade booked"
        raise AssertionError(f"setup failed: {detail}")
    return result.trade
