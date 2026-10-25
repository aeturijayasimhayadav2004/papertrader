"""Daily-bar backtester.

Timing rules (no lookahead):
  * The signal for bar t is computed from data up to and including bar t's close.
  * An order created from that signal fills at bar t+1's OPEN, never at bar t.
  * Stops / targets follow papertrader.exits (the paper broker uses the same rules):
    checked against each bar's low/high, gaps fill at the open, and a trailing
    stop is raised at the close for use from the next bar.
  * Costs come from papertrader.costs and are charged on every fill; open positions
    at the end of a period are charged the exit cost so results are net of costs.
  * An optional market-regime series blocks new entries on days it is False
    (e.g. Nifty below its 200-day average); exits are never blocked.

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

from papertrader.costs import BUY, SELL, CostModel, as_cost_model
from papertrader.exits import ExitRules, intraday_exit
from papertrader.indicators import atr, sma
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
    exit_reason: str  # "signal" | "stop_loss" | "take_profit" | "end_of_period"


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


def market_regime(bench: pd.DataFrame | None, sma_window: int) -> pd.Series | None:
    """True on days the benchmark closed above its `sma_window`-day average (causal)."""
    if bench is None or bench.empty:
        return None
    close = bench["Close"]
    return (close > sma(close, sma_window)).rename("regime_ok")


def simulate_sleeve(
    df: pd.DataFrame,
    signals: pd.Series,
    start: dt.date,
    end: dt.date,
    capital: float,
    cost: CostModel | float,
    stop_loss_pct: float | None = None,
    ticker: str = "",
    exits: ExitRules | None = None,
    regime: pd.Series | None = None,
) -> SleeveResult:
    """Trade one stock long-only with all of `capital`, following `signals`."""
    cm = as_cost_model(cost)
    rules = exits if exits is not None else ExitRules(stop_loss_pct=stop_loss_pct)
    period = df.loc[_ts(start):_ts(end)]
    n = len(period)
    dates = period.index
    if n == 0:
        empty = pd.Series(dtype=float)
        return SleeveResult(empty, empty.astype(bool), [])

    o = period["Open"].to_numpy(float)
    hi = period["High"].to_numpy(float)
    lo = period["Low"].to_numpy(float)
    c = period["Close"].to_numpy(float)
    sig = signals.reindex(dates).fillna(Signal.HOLD.value).to_numpy()
    atr_now = (atr(df, rules.atr_period).reindex(dates).to_numpy(float) if rules.stop_mode == "atr"
               else np.full(n, np.nan))
    can_buy = (np.ones(n, dtype=bool) if regime is None
               else regime.reindex(dates).ffill().fillna(False).to_numpy(bool))

    cash, qty = capital, 0.0
    entry_price = entry_cost = stop = highest = pending_atr = 0.0
    target: float | None = None
    entry_date = signal_date = pending_signal_date = None
    pending: str | None = None
    equity = np.empty(n)
    invested = np.zeros(n, dtype=bool)
    trades: list[BacktestTrade] = []

    def exit_position(i: int, price: float, reason: str) -> None:
        nonlocal cash, qty
        proceeds = qty * price - cm.fee(SELL, qty, price)
        cash += proceeds
        trades.append(BacktestTrade(ticker, signal_date, entry_date, entry_price, dates[i], price,
                                    proceeds / entry_cost - 1.0, reason))
        qty = 0.0

    for i in range(n):
        # 1) fill the order queued at the previous close, at today's open
        if pending == Signal.BUY and qty == 0:
            entry_price = o[i]
            qty = cm.max_qty(cash, entry_price)
            entry_cost = qty * entry_price + cm.fee(BUY, qty, entry_price)
            cash -= entry_cost
            entry_date, signal_date = dates[i], pending_signal_date
            stop = rules.initial_stop(entry_price, pending_atr)
            target = rules.target(entry_price)
            highest = entry_price
        elif pending == Signal.SELL and qty > 0:
            exit_position(i, o[i], "signal")
        pending = None

        # 2) stop-loss / take-profit during the day
        if qty > 0:
            hit = intraday_exit(o[i], hi[i], lo[i], stop, target)
            if hit:
                exit_position(i, *hit)

        # 3) trail the stop at the close (used from tomorrow)
        if qty > 0:
            highest = max(highest, c[i])
            stop = rules.trail(stop, highest)

        # 4) signal at the close -> order for tomorrow's open
        s = sig[i]
        if qty == 0 and s == Signal.BUY and can_buy[i]:
            pending, pending_signal_date, pending_atr = Signal.BUY, dates[i], atr_now[i]
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
    cost: CostModel | float,
    stop_loss_pct: float | None = None,
    exits: ExitRules | None = None,
    regime: pd.Series | None = None,
) -> PortfolioResult:
    sleeve_capital = capital / len(prices)
    sleeves = []
    for ticker, df in prices.items():
        hist = df.loc[:_ts(end)]  # never let the strategy see bars after the period
        sleeves.append(simulate_sleeve(hist, strategy.signals(hist), start, end, sleeve_capital,
                                       cost, stop_loss_pct, ticker, exits, regime))
    return _combine(strategy.name, start, end, capital, sleeves, len(prices))


def buy_and_hold(
    prices: dict[str, pd.DataFrame], start: dt.date, end: dt.date, capital: float, cost: CostModel | float
) -> PortfolioResult:
    cm = as_cost_model(cost)
    sleeve_capital = capital / len(prices)
    sleeves = []
    for ticker, df in prices.items():
        period = df.loc[_ts(start):_ts(end)]
        if period.empty:
            continue
        o0, c_last = period["Open"].iloc[0], period["Close"].iloc[-1]
        qty = cm.max_qty(sleeve_capital, o0)
        eq = qty * period["Close"] + (sleeve_capital - qty * o0 - cm.fee(BUY, qty, o0))
        eq.iloc[-1] -= cm.fee(SELL, qty, c_last)
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
