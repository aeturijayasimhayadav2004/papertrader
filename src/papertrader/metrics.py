"""Performance metrics computed from an equity curve and a trade list."""
from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def total_return(equity: pd.Series, capital: float) -> float:
    return float(equity.iloc[-1] / capital - 1.0) if len(equity) else 0.0


def cagr(equity: pd.Series, capital: float) -> float:
    if len(equity) < 2:
        return 0.0
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    growth = equity.iloc[-1] / capital
    if years <= 0 or growth <= 0:
        return 0.0 if growth > 0 else -1.0
    return float(growth ** (1.0 / years) - 1.0)


def max_drawdown(equity: pd.Series, capital: float) -> float:
    """Largest peak-to-trough fall as a negative fraction (e.g. -0.18)."""
    if not len(equity):
        return 0.0
    peak = equity.cummax().clip(lower=capital)
    return float((equity / peak - 1.0).min())


def daily_returns(equity: pd.Series, capital: float) -> pd.Series:
    prev = equity.shift(1)
    prev.iloc[0] = capital
    return equity / prev - 1.0


def sharpe(equity: pd.Series, capital: float) -> float:
    """Annualised Sharpe ratio of daily returns, risk-free rate 0."""
    rets = daily_returns(equity, capital)
    sd = rets.std(ddof=1)
    if len(rets) < 2 or not sd or math.isnan(sd):
        return 0.0
    return float(rets.mean() / sd * math.sqrt(TRADING_DAYS))


def win_rate(trade_returns: Sequence[float]) -> float | None:
    if not len(trade_returns):
        return None
    return float(np.mean([r > 0 for r in trade_returns]))


def compute_metrics(equity: pd.Series, capital: float, trade_returns: Sequence[float]) -> dict[str, Any]:
    return {
        "total_return": total_return(equity, capital),
        "cagr": cagr(equity, capital),
        "max_drawdown": max_drawdown(equity, capital),
        "sharpe": sharpe(equity, capital),
        "win_rate": win_rate(trade_returns),
        "n_trades": len(trade_returns),
    }
