"""Entry timer: is an opening-range breakout waiting, triggered or expired?

The method (ported from the US trader-entry skill, adapted to NSE hours):

  * The opening range is the high and low of the first `or_minutes` of the
    session. With the default 15 minutes on 5-minute bars that is the 09:15,
    09:20 and 09:25 bars, so the range is set at 09:30 IST.
  * The trigger is the first COMPLETED bar after the range that CLOSES above
    the range high. A wick above the high is not a trigger, and a close exactly
    at the high is not above it.
  * A bar is complete only once the clock passes its start + `bar_minutes`
    (the 10:10 bar completes at 10:15). A bar still forming at `at` never
    counts, however far above the high it is trading.
  * The trigger time is that bar's close time and the price is its close.
  * One trigger per day: the first close above. Later closes do not reset it.
  * Expired: no trigger by `latest_entry` (default 14:30 IST, leaving time to
    manage the trade before the 15:15 flat-by), or (an NSE-side addition, not in
    the US skill) a completed bar closed BELOW the range low before any close
    above the high: the long setup failed, so later breakouts are not taken.

Long side only. It never places, sizes or schedules an order.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from papertrader.agent import market
from papertrader.agent.market import CLOSE, IST, OPEN

STATUSES = ("no_data", "forming_range", "waiting", "triggered", "expired")


@dataclass
class EntryState:
    status: str                              # one of STATUSES
    reason: str                              # one plain sentence, IST times, Rs prices
    day: dt.date | None = None
    at: dt.datetime | None = None            # the clock the check was made at (IST)
    range_high: float | None = None
    range_low: float | None = None
    range_bars: int = 0
    trigger_time: dt.datetime | None = None  # close time of the trigger bar (IST)
    trigger_price: float | None = None
    pct_over: float | None = None            # trigger close vs range high, %
    last_close: float | None = None
    last_bar_start: dt.datetime | None = None
    bars_closed: int = 0

    @property
    def triggered(self) -> bool:
        return self.status == "triggered"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, (dt.date, dt.datetime)):
                d[k] = v.isoformat()
        return d


def hhmm(t: dt.datetime | dt.time) -> str:
    return t.strftime("%H:%M")


def rs(x: float) -> str:
    return f"Rs {x:,.2f}"


def as_ist_index(bars: pd.DataFrame) -> pd.DataFrame:
    """Same bars with a tz-aware IST index (naive timestamps are taken as IST)."""
    idx = pd.DatetimeIndex(bars.index)
    idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
    out = bars.copy()
    out.index = idx
    return out


def session_bars(bars: pd.DataFrame | None, day: dt.date) -> pd.DataFrame:
    """Regular-session bars of one IST day with usable prices, sorted, one per start time."""
    if bars is None or len(bars) == 0 or any(c not in bars.columns for c in ("High", "Low", "Close")):
        return pd.DataFrame()
    try:
        b = as_ist_index(bars)
    except (TypeError, ValueError):
        return pd.DataFrame()
    b = b[b.index.date == day]
    t = b.index.time
    b = b[(t >= OPEN) & (t < CLOSE)]
    b = b.dropna(subset=["High", "Low", "Close"])
    return b[~b.index.duplicated(keep="last")].sort_index()


def completed(bars: pd.DataFrame, at: dt.datetime, bar_minutes: int) -> pd.DataFrame:
    """Bars that have closed by `at`: start + bar_minutes <= at."""
    if bars.empty:
        return bars
    ends = bars.index + pd.Timedelta(minutes=bar_minutes)
    return bars[ends <= at]


def infer_bar_minutes(bars: pd.DataFrame, default: int = 5) -> int:
    if len(bars) < 2:
        return default
    diffs = pd.Series(bars.index).diff().dropna()
    if diffs.empty:
        return default
    mins = int(diffs.min().total_seconds() // 60)
    return mins if mins > 0 else default


def resolve_clock(bars: pd.DataFrame | None, day: dt.date | None, at: dt.datetime | None) -> tuple[dt.date, dt.datetime]:
    """Pick the session day and the clock to judge it at.

    No `day`: the day of `at`; with neither, today if it has bars, else the last
    day that has bars (a weekend check shows Friday's result). A past day with no
    `at` is judged at the end of that day, so every bar counts as closed.
    """
    now = market.now_ist(at)
    if day is None:
        day = now.date()
        if at is None and bars is not None and len(bars):
            try:
                days = sorted(set(as_ist_index(bars).index.date))
            except (TypeError, ValueError):
                days = []
            if days and day not in days:
                day = days[-1]
    if at is None and day < now.date():
        now = dt.datetime.combine(day, dt.time(23, 59), IST)
    return day, now


def check_orb(bars: pd.DataFrame, day: dt.date | None = None, at: dt.datetime | None = None,
              or_minutes: int = 15, bar_minutes: int = 5, latest_entry: dt.time = dt.time(14, 30)) -> EntryState:
    """Opening-range breakout status of one stock's intraday bars at `at` (IST)."""
    day, now = resolve_clock(bars, day, at)
    st = EntryState(status="no_data", reason="", day=day, at=now)
    open_dt = dt.datetime.combine(day, OPEN, IST)
    or_end = open_dt + dt.timedelta(minutes=or_minutes)
    cutoff = dt.datetime.combine(day, latest_entry, IST)

    day_bars = session_bars(bars, day)
    if day_bars.empty and not market.is_trading_day(day):
        st.reason = f"NSE is closed on {day:%a %d %b %Y} ({market.holiday_name(day)})."
        return st
    if now < open_dt:
        st.status = "forming_range"
        st.reason = (f"Market not open yet: NSE opens at {hhmm(OPEN)} IST and the {or_minutes}-minute "
                     f"opening range is set at {hhmm(or_end)}.")
        return st
    if day_bars.empty:
        st.reason = f"No intraday bars for {day:%d %b %Y} yet (Yahoo can lag a few minutes)."
        return st

    done = completed(day_bars, now, bar_minutes)
    st.bars_closed = len(done)
    rng = done[done.index < or_end]
    n_range = max(1, or_minutes // bar_minutes)
    if len(rng):
        st.range_high = round(float(rng["High"].max()), 2)
        st.range_low = round(float(rng["Low"].min()), 2)
        st.range_bars = len(rng)
    if len(done):
        st.last_close = round(float(done["Close"].iloc[-1]), 2)
        st.last_bar_start = done.index[-1].to_pydatetime()

    if now < or_end:
        st.status = "forming_range"
        so_far = (f" So far high {rs(st.range_high)}, low {rs(st.range_low)}." if st.range_high else "")
        st.reason = (f"Opening range still forming: {len(rng)} of {n_range} bars closed; "
                     f"it is set at {hhmm(or_end)} IST.{so_far}")
        return st
    if rng.empty:
        st.reason = f"No bars for the opening range {hhmm(OPEN)}-{hhmm(or_end)} IST."
        return st

    hi, lo = float(rng["High"].max()), float(rng["Low"].min())
    after = done[done.index >= or_end]
    for start, bar in after.iterrows():
        end = start + pd.Timedelta(minutes=bar_minutes)
        if end > cutoff:
            break
        close = float(bar["Close"])
        if close > hi:
            st.status = "triggered"
            st.trigger_time = end.to_pydatetime()
            st.trigger_price = round(close, 2)
            st.pct_over = round(100.0 * (close / hi - 1.0), 2)
            mins = int((now - st.trigger_time).total_seconds() // 60)
            late = f" That was {mins} minutes ago; a late entry is a different trade." if mins > bar_minutes else ""
            st.reason = (f"Triggered at {hhmm(end)} IST at {rs(close)}: the {hhmm(start)} bar closed above the "
                         f"range high {rs(hi)} (+{st.pct_over:.2f}%).{late} Nothing was placed.")
            return st
        if close < lo:
            st.status = "expired"
            st.reason = (f"Expired: the {hhmm(start)} bar closed at {rs(close)}, below the range low {rs(lo)}, "
                         f"before any close above the high {rs(hi)}. The long setup failed for today.")
            return st

    if now >= cutoff:
        st.status = "expired"
        st.reason = (f"Expired: no close above the range high {rs(hi)} by {hhmm(latest_entry)} IST, "
                     f"the last time for a new entry.")
        return st

    st.status = "waiting"
    last = float(done["Close"].iloc[-1])
    gap = hi - last
    st.reason = (f"Waiting: last close {rs(last)} ({hhmm(done.index[-1])} bar) is {rs(abs(gap))} "
                 f"{'under' if gap >= 0 else 'over'} the range high {rs(hi)} ({100.0 * (last / hi - 1):+.2f}%); "
                 f"range low {rs(lo)}.")
    forming = day_bars[(day_bars.index <= now) & (day_bars.index + pd.Timedelta(minutes=bar_minutes) > now)]
    if len(forming):
        f_start = forming.index[-1]
        f_end = f_start + pd.Timedelta(minutes=bar_minutes)
        st.reason += (f" The {hhmm(f_start)} bar is still forming ({rs(float(forming['Close'].iloc[-1]))} so far, "
                      f"closes {hhmm(f_end)}); it does not count until it closes.")
    return st
