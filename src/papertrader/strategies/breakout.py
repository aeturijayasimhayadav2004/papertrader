from __future__ import annotations

import numpy as np
import pandas as pd

from papertrader.indicators import prior_high, prior_low
from papertrader.strategies.base import Signal, Strategy


class Breakout(Strategy):
    """BUY when the close beats the prior N-day high, SELL when it breaks the prior M-day low."""

    name = "breakout"

    def __init__(self, entry_lookback: int = 20, exit_lookback: int = 10):
        self.entry_lookback, self.exit_lookback = entry_lookback, exit_lookback
        self.label = f"{entry_lookback}-day breakout"

    @property
    def warmup(self) -> int:
        return max(self.entry_lookback, self.exit_lookback) + 1

    def frame(self, df: pd.DataFrame) -> pd.DataFrame:
        hi = prior_high(df["High"], self.entry_lookback)
        lo = prior_low(df["Low"], self.exit_lookback)
        close = df["Close"]
        out = pd.DataFrame({"close": close, "prior_high": hi, "prior_low": lo}, index=df.index)
        out["signal"] = np.select(
            [close > hi, close < lo], [Signal.BUY.value, Signal.SELL.value], default=Signal.HOLD.value
        )
        return out

    def explain(self, row: pd.Series) -> str:
        c, hi, lo = row["close"], row["prior_high"], row["prior_low"]
        if pd.isna(hi) or pd.isna(lo):
            return "Not enough history for the breakout levels yet."
        if row["signal"] == Signal.BUY:
            return f"Close Rs {c:,.2f} broke above the {self.entry_lookback}-day high of Rs {hi:,.2f}: breakout."
        if row["signal"] == Signal.SELL:
            return f"Close Rs {c:,.2f} fell below the {self.exit_lookback}-day low of Rs {lo:,.2f}: breakout has failed."
        return (f"Close Rs {c:,.2f} is inside the range (needs > Rs {hi:,.2f} to buy, "
                f"< Rs {lo:,.2f} to sell).")
