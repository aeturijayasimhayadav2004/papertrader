"""Daily-bar backtester.

Timing rules (no lookahead):
  * The signal for bar t is computed from data up to and including bar t's close.
  * An order created from that signal fills at bar t+1's OPEN, never at bar t.
  * Stop-loss is checked against each bar's low. If the bar opens below the stop
    (a gap down) the exit is at the open, otherwise at the stop price.
  * Costs are charged on every fill (per side), and open positions at the end of a
    period are charged the exit cost so results are net of costs.

The portfolio is the universe split into equal-weight sleeves: each stock gets
capital / N and is traded independently by the strategy. Buy-and-hold uses the
same sleeves, bought at the first open of the period.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from papertrader.metrics import compute_metrics
from papertrader.strategies.base import Signal, Strategy


@dataclass(frozen=True)
class BacktestTrade:
    ticker: str
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    return_pct: float  # net of costs on both sides, as a fraction
    exit_reason: str  # "signal" | "stop_loss" | "end_of_period"


@dataclass
class SleeveResult:
    equity: pd.Series
    invested: pd.Series
    trades: list[BacktestTrade]


@dataclass
class PortfolioResult:
    name: str
    start: dt.date
    end: dt.date
    capital: float
    equity: pd.Series
    trades: list[BacktestTrade]
    exposure: float
    metrics: dict[str, Any] = field(default_factory=dict)


def _ts(d: dt.date | pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(d)


def simulate_sleeve(
    df: pd.DataFrame,
    signals: pd.Series,
    start: dt.date,
    end: dt.date,
    capital: float,
    cost: float,
    stop_loss_pct: float | None = None,
    ticker: str = "",
) -> SleeveResult:
    """Trade one stock long-only with all of `capital`, following `signals`."""
    period = df.loc[_ts(start):_ts(end)]
    n = len(period)
    dates = period.index
    if n == 0:
        empty = pd.Series(dtype=float)
        return SleeveResult(empty, empty.astype(bool), [])

    o = period["Open"].to_numpy(float)
    lo = period["Low"].to_numpy(float)
    c = period["Close"].to_numpy(float)
    sig = signals.reindex(dates).fillna(Signal.HOLD.value).to_numpy()
    sl = stop_loss_pct / 100.0 if stop_loss_pct else None

    cash, qty = capital, 0.0
    entry_price = entry_cost = stop = 0.0
    entry_date = signal_date = pending_signal_date = None
    pending: str | None = None
    equity = np.empty(n)
    invested = np.zeros(n, dtype=bool)
    trades: list[BacktestTrade] = []

    def exit_position(i: int, price: float, reason: str) -> None:
        nonlocal cash, qty
        proceeds = qty * price * (1.0 - cost)
        cash += proceeds
        trades.append(BacktestTrade(ticker, signal_date, entry_date, entry_price, dates[i], price,
                                    proceeds / entry_cost - 1.0, reason))
        qty = 0.0

    for i in range(n):
        # 1) fill the order queued at the previous close, at today's open
        if pending == Signal.BUY and qty == 0:
            entry_price = o[i]
            qty = cash / (entry_price * (1.0 + cost))
            entry_cost = qty * entry_price * (1.0 + cost)
            cash -= entry_cost
            entry_date, signal_date = dates[i], pending_signal_date
            stop = entry_price * (1.0 - sl) if sl else 0.0
        elif pending == Signal.SELL and qty > 0:
            exit_position(i, o[i], "signal")
        pending = None

        # 2) stop-loss during the day
        if qty > 0 and sl and lo[i] <= stop:
            exit_position(i, min(o[i], stop), "stop_loss")

        # 3) signal at the close -> order for tomorrow's open
        s = sig[i]
        if qty == 0 and s == Signal.BUY:
            pending, pending_signal_date = Signal.BUY, dates[i]
        elif qty > 0 and s == Signal.SELL:
            pending = Signal.SELL

        equity[i] = cash + qty * c[i]
        invested[i] = qty > 0

    if qty > 0:
        exit_position(n - 1, c[-1], "end_of_period")
        equity[-1] = cash

    return SleeveResult(pd.Series(equity, index=dates), pd.Series(invested, index=dates), trades)


def _combine(name: str, start: dt.date, end: dt.date, capital: float,
             sleeves: list[SleeveResult], n_sleeves: int) -> PortfolioResult:
    sleeve_capital = capital / n_sleeves
    sleeves = [s for s in sleeves if len(s.equity)]
    if not sleeves:
        raise ValueError(f"{name}: no price data between {start} and {end}")
    idx = sleeves[0].equity.index
    for s in sleeves[1:]:
        idx = idx.union(s.equity.index)
    # Before a sleeve's first bar (or if it has no bars at all) its money sits in cash.
    eq = sum(s.equity.reindex(idx).ffill().fillna(sleeve_capital) for s in sleeves)
    eq = eq + sleeve_capital * (n_sleeves - len(sleeves))
    inv = pd.concat([s.invested.reindex(idx).ffill().fillna(False).astype(float) for s in sleeves], axis=1)
    trades = sorted((t for s in sleeves for t in s.trades), key=lambda t: (t.entry_date, t.ticker))
    res = PortfolioResult(name, start, end, capital, eq, trades,
                          exposure=float(inv.sum(axis=1).mean() / n_sleeves))
    res.metrics = compute_metrics(eq, capital, [t.return_pct for t in trades])
    res.metrics["exposure"] = res.exposure
    return res


def run_backtest(
    strategy: Strategy,
    prices: dict[str, pd.DataFrame],
    start: dt.date,
    end: dt.date,
    capital: float,
    cost: float,
    stop_loss_pct: float | None = None,
) -> PortfolioResult:
    sleeve_capital = capital / len(prices)
    sleeves = []
    for ticker, df in prices.items():
        hist = df.loc[:_ts(end)]  # never let the strategy see bars after the period
        sleeves.append(simulate_sleeve(hist, strategy.signals(hist), start, end, sleeve_capital,
                                       cost, stop_loss_pct, ticker))
    return _combine(strategy.name, start, end, capital, sleeves, len(prices))


def buy_and_hold(
    prices: dict[str, pd.DataFrame], start: dt.date, end: dt.date, capital: float, cost: float
) -> PortfolioResult:
    sleeve_capital = capital / len(prices)
    sleeves = []
    for ticker, df in prices.items():
        period = df.loc[_ts(start):_ts(end)]
        if period.empty:
            continue
        o0, c_last = period["Open"].iloc[0], period["Close"].iloc[-1]
        qty = sleeve_capital / (o0 * (1.0 + cost))
        eq = qty * period["Close"]
        eq.iloc[-1] -= qty * c_last * cost
        trade = BacktestTrade(ticker, period.index[0], period.index[0], o0, period.index[-1], c_last,
                              float(eq.iloc[-1] / sleeve_capital - 1.0), "end_of_period")
        sleeves.append(SleeveResult(eq, pd.Series(True, index=period.index), [trade]))
    return _combine("buy_and_hold", start, end, capital, sleeves, len(prices))


def assert_no_lookahead(strategy: Strategy, df: pd.DataFrame, checkpoints: int = 25) -> None:
    """Raise AssertionError if a signal changes when future bars are removed.

    For sampled bars t, signals computed on df[:t+1] must equal signals computed
    on the full df at t. A strategy that peeks at later bars fails this.
    """
    full = strategy.signals(df)
    start = max(strategy.warmup, 1)
    points = np.unique(np.linspace(start, len(df) - 1, num=min(checkpoints, len(df) - start)).astype(int))
    for t in points:
        truncated = strategy.signals(df.iloc[: t + 1])
        if truncated.iloc[-1] != full.iloc[t]:
            raise AssertionError(
                f"{strategy.name}: signal at {df.index[t].date()} is {full.iloc[t]} with future data "
                f"but {truncated.iloc[-1]} without it (lookahead bias)"
            )
