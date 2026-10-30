"""Portfolio-level ("rotation") strategies and their backtester.

Unlike the per-stock strategies in papertrader.strategies, a rotation strategy
looks at the whole universe at once and returns target weights (how much of
the portfolio each stock should be), rebalanced monthly or weekly.
"""
from __future__ import annotations

from papertrader.portfolio.backtest import (
    Fill,
    RotationResult,
    WalkForwardResult,
    backtest_rotation,
    buy_and_hold_universe,
    index_buy_and_hold,
    rebalance_orders,
    walk_forward,
)
from papertrader.portfolio.rotation import REGISTRY, RotationStrategy, build_rotation

__all__ = [
    "REGISTRY",
    "Fill",
    "RotationResult",
    "RotationStrategy",
    "WalkForwardResult",
    "backtest_rotation",
    "build_rotation",
    "buy_and_hold_universe",
    "index_buy_and_hold",
    "rebalance_orders",
    "walk_forward",
]
