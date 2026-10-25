from __future__ import annotations

from typing import Any

from papertrader.strategies.base import Decision, Signal, Strategy
from papertrader.strategies.breakout import Breakout
from papertrader.strategies.ma_crossover import MACrossover
from papertrader.strategies.rsi_bounce import RSIBounce

STRATEGY_CLASSES: dict[str, type[Strategy]] = {
    MACrossover.name: MACrossover,
    RSIBounce.name: RSIBounce,
    Breakout.name: Breakout,
}


def build_strategies(settings: dict[str, dict[str, Any]]) -> list[Strategy]:
    """Instantiate enabled strategies from the `strategies:` config section."""
    out: list[Strategy] = []
    for name, params in settings.items():
        if name not in STRATEGY_CLASSES:
            raise ValueError(f"unknown strategy {name!r}; known: {sorted(STRATEGY_CLASSES)}")
        params = dict(params or {})
        if params.pop("enabled", True):
            out.append(STRATEGY_CLASSES[name](**params))
    return out


__all__ = ["Decision", "Signal", "Strategy", "Breakout", "MACrossover", "RSIBounce", "build_strategies"]
