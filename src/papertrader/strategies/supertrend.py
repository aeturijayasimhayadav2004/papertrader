from __future__ import annotations

import numpy as np
import pandas as pd

from papertrader.indicators import supertrend
from papertrader.strategies.base import Signal, Strategy


class Supertrend(Strategy):
    """BUY when the Supertrend flips to an uptrend, SELL when it flips to a downtrend."""

    name = "supertrend"

    def __init__(self, period: int = 10, multiplier: float = 3.0):
        self.period, self.multiplier = period, multiplier
        self.label = f"Supertrend({period}, {multiplier:g})"

    @property
    def warmup(self) -> int:
        return self.period + 2

    def frame(self, df: pd.DataFrame) -> pd.DataFrame:
        line, direction = supertrend(df, self.period, self.multiplier)
        prev = direction.shift(1)
        up = (direction == 1) & (prev == -1)
        down = (direction == -1) & (prev == 1)
        out = pd.DataFrame({"close": df["Close"], "line": line, "direction": direction}, index=df.index)
        out["signal"] = np.select([up, down], [Signal.BUY.value, Signal.SELL.value], default=Signal.HOLD.value)
        return out

    def explain(self, row: pd.Series) -> str:
        c, line, d = row["close"], row["line"], row["direction"]
        if pd.isna(line) or d == 0:
            return "Supertrend not available yet."
        if row["signal"] == Signal.BUY:
            return f"Close Rs {c:,.2f} crossed above the Supertrend line: the trend turned up."
        if row["signal"] == Signal.SELL:
            return f"Close Rs {c:,.2f} fell below the Supertrend line: the trend turned down."
        trend = "up" if d == 1 else "down"
        return f"No flip today. Trend is {trend}; Supertrend line at Rs {line:,.2f}."
