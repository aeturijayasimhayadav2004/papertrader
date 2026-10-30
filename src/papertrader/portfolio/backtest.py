"""Backtester for rotation (portfolio-level) strategies.

Timing rules (no lookahead):
  * On a rebalance day the strategy sees bars up to and including that day's
    close and returns target weights.
  * Orders are sized at that close (integer shares, equity at that close) and
    filled at the NEXT trading day's open. Sells go first so their cash can pay
    for the buys; a buy is cut down if cash runs short.
  * The first decision is made at the close of the last bar BEFORE the period
    starts, so the portfolio is invested from the first open of the period, the
    same moment equal-weight buy-and-hold buys.
  * Rebalance days are the last trading day of each month ("M") or week ("W").
    Knowing that a day is the last of its month uses the trading calendar only
    (NSE publishes its holiday list in advance), never a price.
  * Every fill pays papertrader.costs charges (CostModel.fee). Cash earns 0.
    Positions are marked at each day's close (last known close if a stock did
    not trade that day). Whatever is held at the end is charged the sell cost
    at the last close, so results are net of costs on both sides.

Buy-and-hold is papertrader.backtest.buy_and_hold: the exact benchmark the
per-stock strategy selection must beat, with the same cost model.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from papertrader.backtest import buy_and_hold
from papertrader.costs import BUY, SELL, CostModel, as_cost_model
from papertrader.metrics import compute_metrics
from papertrader.portfolio.rotation import RotationStrategy
from papertrader.selection import _folds

WeightsCache = dict[pd.Timestamp, dict[str, float]]


@dataclass(frozen=True)
class Fill:
    date: pd.Timestamp
    ticker: str
    side: str      # "BUY" | "SELL"
    qty: int
    price: float
    fee: float
    reason: str    # "rebalance" | "end_of_period"


@dataclass
class RotationResult:
    name: str
    start: dt.date
    end: dt.date
    capital: float
    equity: pd.Series
    fills: list[Fill] = field(default_factory=list)
    exposure: float = 0.0           # average share of equity held in stocks
    turnover: float = 0.0           # one-way traded value per year as a multiple of average equity
    fees: float = 0.0               # total charges paid, INR
    targets: dict[pd.Timestamp, dict[str, float]] = field(default_factory=dict)  # decision date -> weights
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class WalkForwardResult:
    folds: list[dict[str, Any]]     # start, end, strategy_return, bh_return, won
    win_rate: float

    @property
    def wins(self) -> int:
        return sum(1 for f in self.folds if f["won"])


def _ts(d: dt.date | pd.Timestamp | str) -> pd.Timestamp:
    return pd.Timestamp(d)


# --------------------------------------------------------------------------- orders


def rebalance_orders(
    target_weights: dict[str, float],
    positions: dict[str, int],
    prices: dict[str, float],
    equity: float,
    costs: CostModel | float,
    min_trade_value: float = 0.0,
) -> list[tuple[str, str, int]]:
    """Orders that move `positions` to `target_weights` of `equity`, sells first.

    Target shares = floor(weight x equity / cost of one share including buy
    charges), so a fully invested target still leaves room for the fees. A stock
    with a position but no target weight is sold completely. A trade worth less
    than `min_trade_value` is skipped (it is not worth the charges), except a
    full exit or a brand-new position, which always go through. A stock with no
    usable price is left alone. Returns [(ticker, "BUY" | "SELL", qty), ...].
    """
    cm = as_cost_model(costs)
    sells: list[tuple[str, str, int]] = []
    buys: list[tuple[str, str, int]] = []
    for ticker in sorted(set(target_weights) | set(positions)):
        px = prices.get(ticker)
        if px is None or not np.isfinite(px) or px <= 0:
            continue
        have = int(positions.get(ticker, 0))
        w = max(float(target_weights.get(ticker, 0.0)), 0.0)
        want = int(math.floor(w * equity / cm.buy_unit_cost(px))) if w > 0 and equity > 0 else 0
        delta = want - have
        if delta == 0:
            continue
        full_exit = want == 0 and have > 0
        new_entry = have == 0 and want > 0
        if abs(delta) * px < min_trade_value and not (full_exit or new_entry):
            continue
        (sells if delta < 0 else buys).append((ticker, SELL if delta < 0 else BUY, abs(delta)))
    return sells + buys


# --------------------------------------------------------------------------- backtest


def _rebalance_flags(cal: pd.DatetimeIndex, freq: str) -> np.ndarray:
    """True on the last trading day of each month / week in `cal` (never on the final day)."""
    if freq == "M":
        key = cal.year * 100 + cal.month
    else:
        iso = cal.isocalendar()
        key = (iso["year"] * 100 + iso["week"]).to_numpy()
    key = np.asarray(key)
    flags = np.zeros(len(cal), dtype=bool)
    flags[:-1] = key[:-1] != key[1:]
    return flags


def backtest_rotation(
    strategy: RotationStrategy,
    prices: dict[str, pd.DataFrame],
    start: dt.date | pd.Timestamp | str,
    end: dt.date | pd.Timestamp | str,
    costs: CostModel | float,
    capital: float = 1_000_000,
    benchmark: pd.DataFrame | None = None,
    rebalance_band_pct: float = 1.0,
    weights_cache: WeightsCache | None = None,
) -> RotationResult:
    """Simulate `strategy` from `start` to `end` (see the module docstring for the rules).

    rebalance_band_pct: skip a top-up / trim smaller than this % of equity
    (full exits and new positions always trade).
    weights_cache: optional {decision date: weights} shared between runs of the
    SAME strategy on the SAME price data (weights only depend on the date, so
    in-sample, out-of-sample and fold runs can reuse them).
    """
    cm = as_cost_model(costs)
    s, e = _ts(start), _ts(end)
    hist = {t: df.loc[:e] for t, df in prices.items()}  # never let anything see bars after `end`
    hist = {t: df for t, df in hist.items() if not df.empty}
    bench = benchmark.loc[:e] if benchmark is not None else None
    if not hist:
        raise ValueError(f"{strategy.name}: no price data up to {e.date()}")
    all_dates = hist[next(iter(hist))].index
    for df in hist.values():
        all_dates = all_dates.union(df.index)
    cal = all_dates[(all_dates >= s) & (all_dates <= e)]
    if len(cal) == 0:
        raise ValueError(f"{strategy.name}: no price data between {s.date()} and {e.date()}")

    tickers = sorted(hist)
    col = {t: j for j, t in enumerate(tickers)}
    opens = pd.DataFrame({t: hist[t]["Open"] for t in tickers}).reindex(cal)[tickers].to_numpy(float)
    closes_all = pd.DataFrame({t: hist[t]["Close"] for t in tickers}).reindex(all_dates)[tickers].ffill()
    closes = closes_all.reindex(cal).to_numpy(float)
    rebalance = _rebalance_flags(cal, strategy.rebalance)
    band = rebalance_band_pct / 100.0
    cache = weights_cache if weights_cache is not None else {}

    def target(d: pd.Timestamp) -> dict[str, float]:
        if d not in cache:
            cache[d] = strategy.weights(hist, d, bench)
        return cache[d]

    def px_row(row: np.ndarray) -> dict[str, float]:
        return {t: float(row[j]) for t, j in col.items() if np.isfinite(row[j])}

    cash = float(capital)
    pos: dict[str, int] = {}
    fills: list[Fill] = []
    targets: dict[pd.Timestamp, dict[str, float]] = {}
    traded = 0.0
    n = len(cal)
    equity = np.empty(n)
    invested = np.zeros(n)

    # First decision: the last close before the period (or the first close if there is none).
    before = all_dates[all_dates < s]
    pending: list[tuple[str, str, int]] = []
    if len(before):
        d0 = before[-1]
        w0 = target(d0)
        targets[d0] = w0
        pending = rebalance_orders(w0, pos, px_row(closes_all.loc[d0].to_numpy(float)), cash, cm,
                                   band * cash)
    else:
        rebalance[0] = n > 1

    for i, d in enumerate(cal):
        # 1) fill yesterday's orders at today's open
        for ticker, side, qty in pending:
            px = opens[i, col[ticker]]
            if not np.isfinite(px) or px <= 0:
                continue  # no bar today: the order lapses, the next rebalance retries
            if side == SELL:
                qty = min(qty, pos.get(ticker, 0))
                if qty <= 0:
                    continue
                fee = cm.fee(SELL, qty, px)
                cash += qty * px - fee
                pos[ticker] -= qty
                if pos[ticker] == 0:
                    del pos[ticker]
            else:
                qty = min(qty, int(math.floor(max(cash, 0.0) / cm.buy_unit_cost(px))))
                if qty <= 0:
                    continue
                fee = cm.fee(BUY, qty, px)
                cash -= qty * px + fee
                pos[ticker] = pos.get(ticker, 0) + qty
            traded += qty * px
            fills.append(Fill(d, ticker, side, qty, float(px), float(fee), "rebalance"))
        pending = []

        # 2) mark to market at the close
        held = sum(q * closes[i, col[t]] for t, q in pos.items())
        equity[i] = cash + held
        invested[i] = held / equity[i] if equity[i] > 0 else 0.0

        # 3) rebalance decision at the close -> orders for tomorrow's open
        if rebalance[i]:
            w = target(d)
            targets[d] = w
            pending = rebalance_orders(w, pos, px_row(closes[i]), equity[i], cm, band * equity[i])

    # Charge the exit on whatever is still held, at the last close.
    d_last = cal[-1]
    for ticker, qty in list(pos.items()):
        px = closes[-1, col[ticker]]
        fee = cm.fee(SELL, qty, px)
        cash += qty * px - fee
        fills.append(Fill(d_last, ticker, SELL, qty, float(px), float(fee), "end_of_period"))
    equity[-1] = cash

    eq = pd.Series(equity, index=cal, name=strategy.name)
    years = max((cal[-1] - cal[0]).days / 365.25, 1 / 365.25)
    res = RotationResult(strategy.name, cal[0].date(), cal[-1].date(), float(capital), eq, fills,
                         exposure=float(invested.mean()),
                         turnover=float(traded / 2.0 / eq.mean() / years),
                         fees=float(sum(f.fee for f in fills)), targets=targets)
    res.metrics = compute_metrics(eq, capital, [])
    res.metrics.update(n_trades=len(fills), exposure=res.exposure, turnover=res.turnover, fees=res.fees)
    return res


# --------------------------------------------------------------------------- benchmarks


def buy_and_hold_universe(
    prices: dict[str, pd.DataFrame],
    start: dt.date | pd.Timestamp | str,
    end: dt.date | pd.Timestamp | str,
    costs: CostModel | float,
    capital: float = 1_000_000,
) -> RotationResult:
    """Equal-weight buy-and-hold of the universe, net of costs (papertrader.backtest.buy_and_hold).

    Each stock gets capital / N, bought at its first open in the period (a stock
    listed later waits in cash), and the sell cost is charged at the end.
    """
    s, e = _ts(start), _ts(end)
    hist = {t: df.loc[:e] for t, df in prices.items() if not df.loc[s:e].empty}
    pr = buy_and_hold(hist, s.date(), e.date(), capital, as_cost_model(costs))
    res = RotationResult("buy_and_hold", pr.start, pr.end, float(capital), pr.equity, exposure=pr.exposure)
    res.metrics = dict(pr.metrics)
    res.metrics.update(exposure=pr.exposure, turnover=0.0)
    return res


def index_buy_and_hold(
    benchmark: pd.DataFrame,
    start: dt.date | pd.Timestamp | str,
    end: dt.date | pd.Timestamp | str,
    capital: float = 1_000_000,
) -> RotationResult:
    """The index itself (e.g. ^NSEI), bought at the first open of the period, no costs.

    This is a PRICE index: dividends (~1-1.5% a year for the Nifty 50) are not
    included, so it understates what an index fund earned.
    """
    period = benchmark.loc[_ts(start):_ts(end)]
    if period.empty:
        raise ValueError("benchmark has no bars in the period")
    eq = capital * period["Close"] / float(period["Open"].iloc[0])
    res = RotationResult("index", period.index[0].date(), period.index[-1].date(), float(capital), eq,
                         exposure=1.0)
    res.metrics = compute_metrics(eq, capital, [])
    res.metrics.update(exposure=1.0, turnover=0.0)
    return res


def walk_forward(
    strategy: RotationStrategy,
    prices: dict[str, pd.DataFrame],
    start: dt.date | pd.Timestamp | str,
    end: dt.date | pd.Timestamp | str,
    costs: CostModel | float,
    capital: float = 1_000_000,
    benchmark: pd.DataFrame | None = None,
    fold_months: int = 12,
    rebalance_band_pct: float = 1.0,
    weights_cache: WeightsCache | None = None,
) -> WalkForwardResult:
    """Cut [start, end] into `fold_months` folds (as papertrader.selection does) and run each fresh.

    A fold is won when the strategy's total return beats equal-weight
    buy-and-hold over the same fold, both net of costs.
    """
    folds = []
    for fs, fe in _folds(_ts(start).date(), _ts(end).date(), fold_months):
        r = backtest_rotation(strategy, prices, fs, fe, costs, capital, benchmark, rebalance_band_pct,
                              weights_cache)
        b = buy_and_hold_universe(prices, fs, fe, costs, capital)
        sr, br = r.metrics["total_return"], b.metrics["total_return"]
        folds.append({"start": fs, "end": fe, "strategy_return": sr, "bh_return": br, "won": bool(sr > br)})
    rate = sum(f["won"] for f in folds) / len(folds) if folds else 0.0
    return WalkForwardResult(folds, rate)
