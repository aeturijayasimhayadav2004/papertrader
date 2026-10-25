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


def sortino(equity: pd.Series, capital: float) -> float:
    """Annualised Sortino ratio: mean daily return over downside deviation (target 0)."""
    rets = daily_returns(equity, capital)
    if len(rets) < 2:
        return 0.0
    downside = math.sqrt(float((rets.clip(upper=0.0) ** 2).mean()))
    if not downside:
        return 0.0
    return float(rets.mean() / downside * math.sqrt(TRADING_DAYS))


def calmar(equity: pd.Series, capital: float) -> float:
    """CAGR divided by the size of the maximum drawdown."""
    dd = max_drawdown(equity, capital)
    return float(cagr(equity, capital) / abs(dd)) if dd < 0 else 0.0


def max_drawdown_days(equity: pd.Series, capital: float) -> int:
    """Longest stretch, in calendar days, spent below a previous equity peak."""
    if not len(equity):
        return 0
    peak = equity.cummax().clip(lower=capital)
    longest, start = 0, None
    for ts, under in (equity < peak).items():
        if under and start is None:
            start = ts
        elif not under and start is not None:
            longest, start = max(longest, (ts - start).days), None
    if start is not None:
        longest = max(longest, (equity.index[-1] - start).days)
    return int(longest)


def profit_factor(trade_returns: Sequence[float]) -> float | None:
    """Sum of winning trade returns over the size of the sum of losing ones."""
    if not len(trade_returns):
        return None
    wins = sum(r for r in trade_returns if r > 0)
    losses = -sum(r for r in trade_returns if r < 0)
    if not losses:
        return None if not wins else float("inf")
    return float(wins / losses)


def expectancy(trade_returns: Sequence[float]) -> float | None:
    """Average net return per trade."""
    return float(np.mean(trade_returns)) if len(trade_returns) else None


def monte_carlo(trade_returns: Sequence[float], fraction: float = 1.0, n_sims: int = 2000,
                seed: int = 7) -> dict[str, float] | None:
    """Bootstrap the trade sequence (with replacement) to see how fragile the result is.

    Each simulation compounds a random re-draw of the trades, each applied to
    `fraction` of equity (e.g. 1/N for N equal sleeves). It ignores overlap
    between trades, so it is a robustness check, not a forecast.
    """
    r = np.asarray(trade_returns, dtype=float)
    if len(r) < 5:
        return None
    rng = np.random.default_rng(seed)
    draws = rng.choice(r, size=(n_sims, len(r)), replace=True)
    paths = np.cumprod(1.0 + fraction * draws, axis=1)
    finals = paths[:, -1] - 1.0
    peaks = np.maximum.accumulate(np.maximum(paths, 1.0), axis=1)
    dds = (paths / peaks - 1.0).min(axis=1)
    return {
        "prob_loss": float((finals < 0).mean()),
        "return_p5": float(np.percentile(finals, 5)),
        "return_p50": float(np.percentile(finals, 50)),
        "max_drawdown_p95": float(np.percentile(dds, 5)),
    }


def compute_metrics(equity: pd.Series, capital: float, trade_returns: Sequence[float]) -> dict[str, Any]:
    return {
        "total_return": total_return(equity, capital),
        "cagr": cagr(equity, capital),
        "max_drawdown": max_drawdown(equity, capital),
        "sharpe": sharpe(equity, capital),
        "sortino": sortino(equity, capital),
        "calmar": calmar(equity, capital),
        "max_drawdown_days": max_drawdown_days(equity, capital),
        "win_rate": win_rate(trade_returns),
        "profit_factor": profit_factor(trade_returns),
        "expectancy": expectancy(trade_returns),
        "n_trades": len(trade_returns),
    }
