from __future__ import annotations

import numpy as np
import pandas as pd

from papertrader.indicators import bollinger, sma
from papertrader.strategies.base import Signal, Strategy


class BollingerReversion(Strategy):
    """Buy dips in an uptrend: BUY when the close drops below the lower Bollinger band
    while it is still above the long-term average; SELL when it gets back to the middle band."""

    name = "bollinger_reversion"

    def __init__(self, window: int = 20, num_std: float = 2.0, trend_window: int = 200):
        self.window, self.num_std, self.trend_window = window, num_std, trend_window
        self.label = f"Bollinger({window}, {num_std:g}) dip in uptrend"

    @property
    def warmup(self) -> int:
        return max(self.window, self.trend_window) + 1

    def frame(self, df: pd.DataFrame) -> pd.DataFrame:
        close = df["Close"]
        lower, mid, _ = bollinger(close, self.window, self.num_std)
        trend = sma(close, self.trend_window)
        buy = (close < lower) & (close > trend)
        sell = close > mid
        out = pd.DataFrame({"close": close, "lower": lower, "mid": mid, "trend": trend}, index=df.index)
        out["signal"] = np.select([buy, sell], [Signal.BUY.value, Signal.SELL.value], default=Signal.HOLD.value)
        return out

    def explain(self, row: pd.Series) -> str:
        c, lo, mid, tr = row["close"], row["lower"], row["mid"], row["trend"]
        if pd.isna(lo) or pd.isna(tr):
            return "Bands or long-term average not available yet."
        if row["signal"] == Signal.BUY:
            return (f"Close Rs {c:,.2f} dipped below the lower band (Rs {lo:,.2f}) while staying above the "
                    f"{self.trend_window}-day average (Rs {tr:,.2f}): buying the dip in an uptrend.")
        if row["signal"] == Signal.SELL:
            return f"Close Rs {c:,.2f} is back above the middle band (Rs {mid:,.2f}): the dip has recovered."
        where = "above" if c > tr else "below"
        return (f"No setup. Close Rs {c:,.2f}, lower band Rs {lo:,.2f}, "
                f"{where} the {self.trend_window}-day average.")
