from __future__ import annotations

from typing import Any

from papertrader.strategies.base import Decision, Signal, Strategy
from papertrader.strategies.bollinger_reversion import BollingerReversion
from papertrader.strategies.breakout import Breakout
from papertrader.strategies.ma_crossover import MACrossover
from papertrader.strategies.momentum import Momentum
from papertrader.strategies.rsi_bounce import RSIBounce
from papertrader.strategies.supertrend import Supertrend

STRATEGY_CLASSES: dict[str, type[Strategy]] = {
    MACrossover.name: MACrossover,
    RSIBounce.name: RSIBounce,
    Breakout.name: Breakout,
    Supertrend.name: Supertrend,
    BollingerReversion.name: BollingerReversion,
    Momentum.name: Momentum,
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


__all__ = ["Decision", "Signal", "Strategy", "BollingerReversion", "Breakout", "MACrossover", "Momentum",
           "RSIBounce", "Supertrend", "build_strategies"]
