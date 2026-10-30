"""Rotation strategies: pick a basket of stocks from the whole universe each period.

Every strategy here answers one question at a rebalance date: "using only prices
up to and including today's close, what share of the portfolio should each stock
get?" The answer is a dict {ticker: weight}; weights add up to at most 1 and the
rest is held as cash. The backtester (papertrader.portfolio.backtest) trades the
change at the NEXT open, so a strategy can never act on a price it could not
have seen.

All parameters are fixed in advance from well-known research and index rules,
not fitted to Indian data (see each class's docstring for the source):

  momentum_12_1         top 10 by 12-1 momentum, equal weight, monthly
  momentum_12_1_regime  the same, but all cash while the Nifty 50 is below its 200-day average
  momentum_12_1_invvol  the same picks as momentum_12_1, weighted by 1 / 60-day volatility
  dual_momentum         top 10 by 12-month return, keeping only names that rose over 12 months
  low_volatility        15 calmest stocks by 252-day volatility, equal weight, monthly
  trend_sma200          every stock above its 200-day average, 10% each at most, monthly
  momentum_lowvol       top 10 by the sum of a momentum rank and a low-volatility rank
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

TRADING_DAYS = 252
STALE_DAYS = 7  # a stock whose last bar is older than this (calendar days) is skipped


# --------------------------------------------------------------------------- features


def _close_tail(df: pd.DataFrame, as_of: pd.Timestamp, n: int) -> tuple[np.ndarray, pd.Timestamp | None]:
    """The last `n` closes on or before `as_of` (fewer if history is short), and the last bar's date."""
    idx = df.index
    pos = int(idx.searchsorted(as_of, side="right"))
    if pos == 0:
        return np.empty(0), None
    close = df["Close"].to_numpy(float)
    return close[max(0, pos - n):pos], idx[pos - 1]


def _vol(closes: np.ndarray, window: int) -> float:
    """Annualised standard deviation of the last `window` daily log returns."""
    if len(closes) < window + 1:
        return math.nan
    r = np.diff(np.log(closes[-(window + 1):]))
    return float(np.std(r, ddof=1) * math.sqrt(TRADING_DAYS))


def features_at(prices: dict[str, pd.DataFrame], as_of: pd.Timestamp, *, lookback: int = 252,
                skip: int = 21, vol_short: int = 60, vol_long: int = 252, sma_window: int = 200,
                min_bars: int = 253) -> pd.DataFrame:
    """One row per stock with enough fresh history at `as_of`; only bars <= as_of are used.

    Columns: close, mom (return from `lookback` to `skip` bars ago, the classic
    "12-1" momentum), ret (return over the last `lookback` bars), vol_short,
    vol_long (annualised), sma and above_sma (close above its `sma_window` average).
    """
    as_of = pd.Timestamp(as_of)
    need = max(lookback + 1, vol_long + 1, vol_short + 1, sma_window, min_bars)
    rows: dict[str, dict[str, float]] = {}
    for ticker, df in prices.items():
        c, last = _close_tail(df, as_of, need)
        if last is None or len(c) < min_bars or (as_of - last).days > STALE_DAYS:
            continue
        now = c[-1]
        row = {
            "close": now,
            "mom": c[-1 - skip] / c[-1 - lookback] - 1.0 if len(c) > lookback else math.nan,
            "ret": now / c[-1 - lookback] - 1.0 if len(c) > lookback else math.nan,
            "vol_short": _vol(c, vol_short),
            "vol_long": _vol(c, vol_long),
            "sma": float(c[-sma_window:].mean()) if len(c) >= sma_window else math.nan,
        }
        row["above_sma"] = float(now > row["sma"]) if not math.isnan(row["sma"]) else math.nan
        rows[ticker] = row
    cols = ["close", "mom", "ret", "vol_short", "vol_long", "sma", "above_sma"]
    return pd.DataFrame.from_dict(rows, orient="index", columns=cols)


def regime_at(benchmark: pd.DataFrame | None, as_of: pd.Timestamp, window: int = 200) -> tuple[bool | None, str]:
    """(is the benchmark above its `window`-day average at as_of's close?, reason). None = unknown."""
    if benchmark is None or benchmark.empty:
        return None, "no benchmark data"
    c, last = _close_tail(benchmark, pd.Timestamp(as_of), window)
    if last is None or len(c) < window or (pd.Timestamp(as_of) - last).days > STALE_DAYS:
        return None, "not enough recent benchmark history"
    avg = float(c.mean())
    ok = bool(c[-1] > avg)
    return ok, f"Nifty 50 {c[-1]:,.0f} {'above' if ok else 'below'} its {window}-day average {avg:,.0f}"


# --------------------------------------------------------------------------- base class


@dataclass
class _Decision:
    weights: dict[str, float]
    reasons: dict[str, str]
    note: str = ""


class RotationStrategy:
    """Base class. Subclasses implement `_decide(features, regime_ok, regime_note)`.

    name / label      id and plain-English title
    warmup_days       bars a stock needs before it can be picked
    rebalance         "M" = last trading day of each month, "W" = last trading day of each week
    """

    name: str = ""
    label: str = ""
    source: str = ""

    def __init__(self, rebalance: str = "M", **params: Any):
        if rebalance not in ("M", "W"):
            raise ValueError("rebalance must be 'M' (monthly) or 'W' (weekly)")
        self.rebalance = rebalance
        self.params = params

    # subclasses override these
    @property
    def warmup_days(self) -> int:
        return 253

    uses_regime = False

    def _decide(self, feat: pd.DataFrame, regime_ok: bool | None, regime_note: str) -> _Decision:
        raise NotImplementedError

    # public API
    def decide(self, prices: dict[str, pd.DataFrame], as_of: pd.Timestamp,
               benchmark: pd.DataFrame | None = None) -> _Decision:
        as_of = pd.Timestamp(as_of)
        feat = features_at(prices, as_of, min_bars=self.warmup_days, **self._feature_kwargs())
        regime_ok, regime_note = (regime_at(benchmark, as_of) if self.uses_regime else (None, ""))
        dec = self._decide(feat, regime_ok, regime_note)
        total = sum(dec.weights.values())
        if total > 1.0 + 1e-9:
            dec.weights = {t: w / total for t, w in dec.weights.items()}
        dec.weights = {t: float(w) for t, w in dec.weights.items() if w > 0}
        return dec

    def weights(self, prices: dict[str, pd.DataFrame], as_of: pd.Timestamp,
                benchmark: pd.DataFrame | None = None) -> dict[str, float]:
        """Target weight per stock using only bars up to and including as_of's close.

        Weights sum to at most 1; whatever is left over is cash.
        """
        return self.decide(prices, as_of, benchmark).weights

    def explain(self, prices: dict[str, pd.DataFrame], as_of: pd.Timestamp,
                benchmark: pd.DataFrame | None = None) -> dict[str, str]:
        """Plain-English reason for each holding (key "_cash" explains an all-cash decision)."""
        dec = self.decide(prices, as_of, benchmark)
        out = {t: dec.reasons.get(t, "") for t in dec.weights}
        if dec.note:
            out["_cash" if not dec.weights else "_note"] = dec.note
        return out

    def _feature_kwargs(self) -> dict[str, int]:
        return {}

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name}, {self.params})"


def _equal(names: list[str], slots: int) -> dict[str, float]:
    return {t: 1.0 / slots for t in names} if slots else {}


def _pct(v: float) -> str:
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:+.1%}"


# --------------------------------------------------------------------------- candidates


class MomentumRotation(RotationStrategy):
    """Cross-sectional momentum: hold the stocks that rose most over the past year.

    Score = return from 12 months ago to 1 month ago ("12-1"): skipping the last
    month avoids the short-term reversal effect. Top `top_n` names, equal weight,
    rebalanced monthly. Source: Jegadeesh & Titman (1993), "Returns to Buying
    Winners and Selling Losers"; the 12-1 definition is the standard one used by
    Fama-French / AQR (Asness, Moskowitz & Pedersen 2013). Top 10 of ~100 is the
    usual top-decile portfolio.

    Options:
      regime_filter   all cash while the Nifty 50 closes below its 200-day average
                      (Faber 2007, "A Quantitative Approach to Tactical Asset Allocation").
                      With no benchmark data the strategy stays in cash (it cannot confirm).
      weighting       "equal" or "inverse_vol" (weight ~ 1 / 60-day volatility, capped
                      at 2 / top_n per name; common risk-parity practice).
      score           "mom" (12-1) or "ret" (plain 12-month return).
      absolute        keep only names whose score is above 0; empty slots stay cash
                      (Antonacci 2014, "Dual Momentum Investing").
    """

    name = "momentum_12_1"
    label = "12-1 momentum, top 10"

    def __init__(self, rebalance: str = "M", top_n: int = 10, lookback: int = 252, skip: int = 21,
                 regime_filter: bool = False, weighting: str = "equal", score: str = "mom",
                 absolute: bool = False, vol_window: int = 60):
        super().__init__(rebalance, top_n=top_n, lookback=lookback, skip=skip, regime_filter=regime_filter,
                         weighting=weighting, score=score, absolute=absolute, vol_window=vol_window)
        if weighting not in ("equal", "inverse_vol"):
            raise ValueError("weighting must be 'equal' or 'inverse_vol'")
        if score not in ("mom", "ret"):
            raise ValueError("score must be 'mom' or 'ret'")
        self.top_n, self.lookback, self.skip = top_n, lookback, skip
        self.regime_filter, self.weighting, self.score, self.absolute = regime_filter, weighting, score, absolute
        self.vol_window = vol_window
        self.uses_regime = regime_filter

    @property
    def warmup_days(self) -> int:
        return self.lookback + 1

    def _feature_kwargs(self) -> dict[str, int]:
        return {"lookback": self.lookback, "skip": self.skip, "vol_short": self.vol_window}

    def _decide(self, feat, regime_ok, regime_note):
        if self.regime_filter and not regime_ok:
            why = regime_note if regime_ok is False else f"market regime unknown ({regime_note})"
            return _Decision({}, {}, f"All cash: {why}.")
        what = "12-1 momentum" if self.score == "mom" else "12-month return"
        ranked = feat[self.score].dropna().sort_values(ascending=False)
        n_all = len(ranked)
        top = ranked.head(self.top_n)
        dropped = []
        if self.absolute:
            dropped = list(top[top <= 0].index)
            top = top[top > 0]
        names = list(top.index)
        if not names:
            return _Decision({}, {}, f"All cash: no stock had a positive {what}.")
        if self.weighting == "inverse_vol":
            inv = 1.0 / feat.loc[names, "vol_short"].clip(lower=1e-6)
            inv = inv.fillna(inv.mean())
            w = (inv / inv.sum()).clip(upper=2.0 / self.top_n)
            weights = {t: float(w[t]) * len(names) / self.top_n for t in names}
        else:
            weights = _equal(names, self.top_n)
        reasons = {}
        for rank, t in enumerate(names, start=1):
            r = feat.loc[t]
            reasons[t] = (f"{what} {_pct(r[self.score])} ranks {rank} of {n_all}; "
                          f"{self.vol_window}-day volatility {r['vol_short']:.0%}"
                          + (f"; {regime_note}" if self.regime_filter else ""))
        note = ""
        if dropped:
            note = f"{len(dropped)} of the top {self.top_n} had a negative {what}; their slots are cash."
        return _Decision(weights, reasons, note)


class LowVolRotation(RotationStrategy):
    """Low volatility: hold the calmest stocks.

    Pick the `top_n` stocks with the lowest annualised volatility of daily returns
    over the last `vol_window` days, equal weight, monthly. Source: the low-risk
    anomaly (Blitz & van Vliet 2007; Baker, Bradley & Wurgler 2011) and NSE's
    own Nifty 100 Low Volatility 30 index, which ranks by 1-year daily volatility.
    """

    name = "low_volatility"
    label = "Low volatility, 15 calmest"

    def __init__(self, rebalance: str = "M", top_n: int = 15, vol_window: int = 252):
        super().__init__(rebalance, top_n=top_n, vol_window=vol_window)
        self.top_n, self.vol_window = top_n, vol_window

    @property
    def warmup_days(self) -> int:
        return self.vol_window + 1

    def _feature_kwargs(self):
        return {"vol_long": self.vol_window}

    def _decide(self, feat, regime_ok, regime_note):
        ranked = feat["vol_long"].dropna().sort_values()
        names = list(ranked.head(self.top_n).index)
        reasons = {t: f"{self.vol_window}-day volatility {ranked[t]:.0%}, rank {i} calmest of {len(ranked)}"
                   for i, t in enumerate(names, start=1)}
        return _Decision(_equal(names, self.top_n), reasons, "" if names else "All cash: no stock had enough history.")


class TrendRotation(RotationStrategy):
    """Trend following per stock: own everything that is above its 200-day average.

    Every stock closing above its `sma_window`-day simple average is held at
    equal weight, at most `max_weight` each (so fewer than 10 uptrends leaves
    cash), monthly. Source: Faber (2007) monthly 10-month / 200-day rule;
    the 200-day average is the most widely watched trend line in practice.
    """

    name = "trend_sma200"
    label = "Trend: all stocks above 200-day average"

    def __init__(self, rebalance: str = "M", sma_window: int = 200, max_weight: float = 0.10):
        super().__init__(rebalance, sma_window=sma_window, max_weight=max_weight)
        self.sma_window, self.max_weight = sma_window, max_weight

    @property
    def warmup_days(self) -> int:
        return self.sma_window

    def _feature_kwargs(self):
        return {"sma_window": self.sma_window}

    def _decide(self, feat, regime_ok, regime_note):
        up = feat[feat["above_sma"] == 1.0]
        names = list(up.index)
        if not names:
            return _Decision({}, {}, f"All cash: no stock is above its {self.sma_window}-day average.")
        w = min(1.0 / len(names), self.max_weight)
        reasons = {t: f"close {up.loc[t, 'close']:,.2f} is {up.loc[t, 'close'] / up.loc[t, 'sma'] - 1:+.1%} "
                      f"above its {self.sma_window}-day average ({len(names)} of {len(feat)} stocks in uptrends)"
                   for t in names}
        return _Decision({t: w for t in names}, reasons)


class MomentumLowVolRotation(RotationStrategy):
    """Momentum plus low volatility: strong AND calm stocks.

    Rank every stock by 12-1 momentum (high is good) and by 252-day volatility
    (low is good); add the two ranks; hold the `top_n` best totals, equal
    weight, monthly. Source: multi-factor blends such as NSE's Nifty Alpha Low
    Volatility 30 and Nifty 200 Momentum 30 (which also adjusts momentum for
    volatility); combining two low-correlated factors is standard practice
    (Asness et al. 2013).
    """

    name = "momentum_lowvol"
    label = "Momentum + low volatility blend, top 10"

    def __init__(self, rebalance: str = "M", top_n: int = 10, lookback: int = 252, skip: int = 21,
                 vol_window: int = 252):
        super().__init__(rebalance, top_n=top_n, lookback=lookback, skip=skip, vol_window=vol_window)
        self.top_n, self.lookback, self.skip, self.vol_window = top_n, lookback, skip, vol_window

    @property
    def warmup_days(self) -> int:
        return max(self.lookback, self.vol_window) + 1

    def _feature_kwargs(self):
        return {"lookback": self.lookback, "skip": self.skip, "vol_long": self.vol_window}

    def _decide(self, feat, regime_ok, regime_note):
        f = feat.dropna(subset=["mom", "vol_long"])
        if f.empty:
            return _Decision({}, {}, "All cash: no stock had enough history.")
        mom_rank = f["mom"].rank(ascending=False, method="first")
        vol_rank = f["vol_long"].rank(ascending=True, method="first")
        total = (mom_rank + vol_rank).sort_values(kind="stable")
        names = list(total.head(self.top_n).index)
        reasons = {t: f"momentum {_pct(f.loc[t, 'mom'])} (rank {int(mom_rank[t])}) + volatility "
                      f"{f.loc[t, 'vol_long']:.0%} (rank {int(vol_rank[t])}) = combined rank {i} of {len(f)}"
                   for i, t in enumerate(names, start=1)}
        return _Decision(_equal(names, self.top_n), reasons)


# --------------------------------------------------------------------------- registry


@dataclass(frozen=True)
class _Spec:
    cls: type[RotationStrategy]
    params: dict[str, Any]
    label: str


REGISTRY: dict[str, _Spec] = {
    "momentum_12_1": _Spec(MomentumRotation, {}, "12-1 momentum, top 10, monthly"),
    "momentum_12_1_regime": _Spec(MomentumRotation, {"regime_filter": True},
                                  "12-1 momentum, top 10, cash when Nifty < 200-day avg"),
    "momentum_12_1_invvol": _Spec(MomentumRotation, {"weighting": "inverse_vol"},
                                  "12-1 momentum, top 10, inverse-volatility weights"),
    "dual_momentum": _Spec(MomentumRotation, {"score": "ret", "absolute": True},
                           "Dual momentum: top 10 by 12-month return, only if positive"),
    "low_volatility": _Spec(LowVolRotation, {}, "Low volatility: 15 calmest (252-day vol)"),
    "trend_sma200": _Spec(TrendRotation, {}, "Trend: every stock above its 200-day avg, max 10% each"),
    "momentum_lowvol": _Spec(MomentumLowVolRotation, {}, "Momentum + low-vol rank blend, top 10"),
}


def build_rotation(name: str, params: dict | None = None) -> RotationStrategy:
    """Build a registered rotation strategy; `params` override its defaults."""
    if name not in REGISTRY:
        raise KeyError(f"unknown rotation strategy {name!r}; choose from {', '.join(REGISTRY)}")
    spec = REGISTRY[name]
    strat = spec.cls(**{**spec.params, **(params or {})})
    strat.name = name
    strat.label = spec.label
    return strat
