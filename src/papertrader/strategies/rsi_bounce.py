from __future__ import annotations

import numpy as np
import pandas as pd

from papertrader.indicators import rsi
from papertrader.strategies.base import Signal, Strategy


class RSIBounce(Strategy):
    """BUY when RSI climbs back above the oversold level, SELL when RSI is overbought."""

    name = "rsi_bounce"

    def __init__(self, period: int = 14, oversold: float = 30, exit_above: float = 70):
        if not 0 < oversold < exit_above < 100:
            raise ValueError("need 0 < oversold < exit_above < 100")
        self.period, self.oversold, self.exit_above = period, oversold, exit_above
        self.label = f"RSI({period}) oversold bounce"

    @property
    def warmup(self) -> int:
        return self.period + 2

    def frame(self, df: pd.DataFrame) -> pd.DataFrame:
        r = rsi(df["Close"], self.period)
        prev = r.shift(1)
        buy = (r >= self.oversold) & (prev < self.oversold)
        sell = r > self.exit_above
        out = pd.DataFrame({"close": df["Close"], "rsi": r, "rsi_prev": prev}, index=df.index)
        out["signal"] = np.select([buy, sell], [Signal.BUY.value, Signal.SELL.value], default=Signal.HOLD.value)
        return out

    def explain(self, row: pd.Series) -> str:
        r, p = row["rsi"], row["rsi_prev"]
        if pd.isna(r):
            return "RSI not available yet."
        name = f"RSI({self.period})"
        if row["signal"] == Signal.BUY:
            return (f"{name} rose from {p:.1f} to {r:.1f}, back above {self.oversold:g} after being oversold: "
                    "a bounce may be starting.")
        if row["signal"] == Signal.SELL:
            return f"{name} is {r:.1f}, above {self.exit_above:g}: the stock looks overbought, time to take profit."
        return f"{name} is {r:.1f}: no oversold bounce and not overbought."
