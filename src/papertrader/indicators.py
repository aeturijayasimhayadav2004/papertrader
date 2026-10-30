"""Technical indicators. All are causal: the value at bar t uses bars <= t only."""
from __future__ import annotations

import numpy as np
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


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    return pd.concat([df["High"] - df["Low"], (df["High"] - prev_close).abs(),
                      (df["Low"] - prev_close).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average True Range."""
    return true_range(df).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def bollinger(close: pd.Series, window: int = 20, num_std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(lower, middle, upper) bands."""
    mid = sma(close, window)
    sd = close.rolling(window, min_periods=window).std(ddof=0)
    return mid - num_std * sd, mid, mid + num_std * sd


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> tuple[pd.Series, pd.Series]:
    """(line, direction) with direction +1 = uptrend, -1 = downtrend, 0 = not ready.

    Standard recursive definition: the final bands only move in the trend's favour,
    and the trend flips when the close crosses the active band.
    """
    a = atr(df, period).to_numpy(float)
    hl2 = ((df["High"] + df["Low"]) / 2.0).to_numpy(float)
    close = df["Close"].to_numpy(float)
    n = len(df)
    upper, lower = hl2 + multiplier * a, hl2 - multiplier * a
    line, direction = np.full(n, np.nan), np.zeros(n)
    fu = fl = np.nan
    for i in range(n):
        if np.isnan(a[i]):
            continue
        if np.isnan(fu):
            fu, fl = upper[i], lower[i]
            direction[i] = 1.0 if close[i] > hl2[i] else -1.0
        else:
            fu = upper[i] if (upper[i] < fu or close[i - 1] > fu) else fu
            fl = lower[i] if (lower[i] > fl or close[i - 1] < fl) else fl
            prev = direction[i - 1]
            if prev == 1.0:
                direction[i] = -1.0 if close[i] < fl else 1.0
            else:
                direction[i] = 1.0 if close[i] > fu else -1.0
        line[i] = fl if direction[i] == 1.0 else fu
    return pd.Series(line, index=df.index), pd.Series(direction, index=df.index)
