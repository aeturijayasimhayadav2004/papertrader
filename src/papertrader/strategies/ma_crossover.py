from __future__ import annotations

import numpy as np
import pandas as pd

from papertrader.indicators import sma
from papertrader.strategies.base import Signal, Strategy


class MACrossover(Strategy):
    """BUY when the fast SMA crosses above the slow SMA, SELL when it crosses below."""

    name = "ma_crossover"

    def __init__(self, fast: int = 20, slow: int = 50):
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        self.fast, self.slow = fast, slow
        self.label = f"{fast}/{slow} MA crossover"

    @property
    def warmup(self) -> int:
        return self.slow + 1

    def frame(self, df: pd.DataFrame) -> pd.DataFrame:
        close = df["Close"]
        fast, slow = sma(close, self.fast), sma(close, self.slow)
        up = (fast > slow) & (fast.shift(1) <= slow.shift(1))
        down = (fast < slow) & (fast.shift(1) >= slow.shift(1))
        out = pd.DataFrame({"close": close, "fast": fast, "slow": slow}, index=df.index)
        out["signal"] = np.select([up, down], [Signal.BUY.value, Signal.SELL.value], default=Signal.HOLD.value)
        return out

    def explain(self, row: pd.Series) -> str:
        f, s = row["fast"], row["slow"]
        if pd.isna(f) or pd.isna(s):
            return "Moving averages not available yet."
        fa, sa = f"{self.fast}-day average (Rs {f:,.2f})", f"{self.slow}-day average (Rs {s:,.2f})"
        if row["signal"] == Signal.BUY:
            return f"The {fa} crossed above the {sa}: a new uptrend may be starting."
        if row["signal"] == Signal.SELL:
            return f"The {fa} crossed below the {sa}: the uptrend may be ending."
        side = "above" if f > s else "below"
        return f"No crossover today. The {fa} is {side} the {sa}."
