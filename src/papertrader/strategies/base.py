"""Strategy interface.

A strategy turns a daily OHLCV frame into one signal per bar. The signal at bar t
may only use data up to and including bar t's close; the order is filled at the
next bar's open by the backtester / paper broker, never by the strategy.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Decision:
    signal: Signal
    reason: str


class Strategy(ABC):
    name: str = ""
    label: str = ""

    @property
    @abstractmethod
    def warmup(self) -> int:
        """Bars of history needed before the first meaningful signal."""

    @abstractmethod
    def frame(self, df: pd.DataFrame) -> pd.DataFrame:
        """Indicator columns plus a 'signal' column (BUY/SELL/HOLD strings), one row per bar."""

    @abstractmethod
    def explain(self, row: pd.Series) -> str:
        """Plain-English reason for the signal in one row of frame()."""

    def signals(self, df: pd.DataFrame) -> pd.Series:
        return self.frame(df)["signal"]

    def decide(self, df: pd.DataFrame) -> Decision:
        """Decision for the latest bar in df."""
        if len(df) < self.warmup:
            return Decision(Signal.HOLD, f"Not enough price history yet ({len(df)} days, need {self.warmup}).")
        row = self.frame(df).iloc[-1]
        return Decision(Signal(row["signal"]), self.explain(row))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name})"
