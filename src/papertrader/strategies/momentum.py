from __future__ import annotations

import numpy as np
import pandas as pd

from papertrader.indicators import sma
from papertrader.strategies.base import Signal, Strategy


class Momentum(Strategy):
    """Time-series momentum: hold while the stock's return over the lookback (skipping the
    most recent `skip` days) is above `min_return_pct` and the close is above its trend average."""

    name = "momentum"

    def __init__(self, lookback: int = 126, skip: int = 5, min_return_pct: float = 10.0, trend_window: int = 100):
        if skip >= lookback:
            raise ValueError("skip must be shorter than lookback")
        self.lookback, self.skip, self.min_return_pct, self.trend_window = lookback, skip, min_return_pct, trend_window
        self.label = f"{lookback}-day momentum"

    @property
    def warmup(self) -> int:
        return max(self.lookback, self.trend_window) + 1

    def frame(self, df: pd.DataFrame) -> pd.DataFrame:
        close = df["Close"]
        mom = close.shift(self.skip) / close.shift(self.lookback) - 1.0
        trend = sma(close, self.trend_window)
        strong = (mom > self.min_return_pct / 100.0) & (close > trend)
        weak = (mom < 0) | (close < trend)
        out = pd.DataFrame({"close": close, "momentum": mom, "trend": trend}, index=df.index)
        out["signal"] = np.select([strong, weak], [Signal.BUY.value, Signal.SELL.value], default=Signal.HOLD.value)
        return out

    def explain(self, row: pd.Series) -> str:
        m, c, tr = row["momentum"], row["close"], row["trend"]
        if pd.isna(m) or pd.isna(tr):
            return "Not enough history for momentum yet."
        side = "above" if c > tr else "below"
        base = (f"{self.lookback}-day return {m:+.1%}; close Rs {c:,.2f} is {side} the "
                f"{self.trend_window}-day average (Rs {tr:,.2f}).")
        if row["signal"] == Signal.BUY:
            return f"Strong momentum: {base}"
        if row["signal"] == Signal.SELL:
            return f"Momentum lost: {base}"
        return f"Momentum positive but below the {self.min_return_pct:g}% bar: {base}"
