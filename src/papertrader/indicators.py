"""Technical indicators. All are causal: the value at bar t uses bars <= t only."""
from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI (exponential smoothing with alpha = 1/period)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100.0 - 100.0 / (1.0 + rs)
    # No losses in the window -> RSI is 100 (rs is inf, or nan when there were no moves at all).
    out = out.where(avg_loss != 0, 100.0)
    return out.where(avg_gain.notna())


def prior_high(high: pd.Series, window: int) -> pd.Series:
    """Highest high of the previous `window` bars, excluding the current bar."""
    return high.rolling(window, min_periods=window).max().shift(1)


def prior_low(low: pd.Series, window: int) -> pd.Series:
    """Lowest low of the previous `window` bars, excluding the current bar."""
    return low.rolling(window, min_periods=window).min().shift(1)
