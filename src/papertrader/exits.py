"""Exit rules shared by the backtester and the paper broker, so both behave the same.

  * Initial stop: a fixed % below the fill, or (ATR mode) `atr_multiple` x ATR
    below the fill, never wider than the fixed % (it is the maximum loss).
    The ATR is the one known at the signal close, never a later value.
  * Trailing stop (optional): after each close the stop is raised to
    `trailing_pct` below the highest close since entry. It never moves down
    and takes effect from the next bar.
  * Take profit (optional): exit when the high reaches `take_profit_pct` above
    the fill.
  * Intraday order on a daily bar is unknown, so when the stop and the target
    are both inside one bar we assume the stop hit first (the pessimistic case).
    Gaps fill at the open.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ExitRules:
    stop_loss_pct: float | None = 5.0
    stop_mode: str = "fixed"           # fixed | atr
    atr_multiple: float = 2.5
    trailing_pct: float | None = None
    take_profit_pct: float | None = None
    atr_period: int = 14

    def initial_stop(self, fill_price: float, atr_at_signal: float | None = None) -> float:
        if not self.stop_loss_pct:
            return 0.0
        fixed = fill_price * (1.0 - self.stop_loss_pct / 100.0)
        if self.stop_mode == "atr" and atr_at_signal and not math.isnan(atr_at_signal):
            return max(fill_price - self.atr_multiple * atr_at_signal, fixed)
        return fixed

    def stop_distance(self, price: float, atr_at_signal: float | None = None) -> float:
        """Expected loss per share if stopped out, for risk-based sizing."""
        stop = self.initial_stop(price, atr_at_signal)
        return price - stop if stop else price * 0.10

    def target(self, fill_price: float) -> float | None:
        return fill_price * (1.0 + self.take_profit_pct / 100.0) if self.take_profit_pct else None

    def trail(self, stop: float, highest_close: float) -> float:
        if not self.trailing_pct:
            return stop
        return max(stop, highest_close * (1.0 - self.trailing_pct / 100.0))


def intraday_exit(open_: float, high: float, low: float, stop: float, target: float | None) -> tuple[float, str] | None:
    """(exit price, reason) if the stop or target was hit on this bar, else None."""
    if stop and low <= stop:
        return min(open_, stop), "stop_loss"
    if target and high >= target:
        return max(open_, target), "take_profit"
    return None
