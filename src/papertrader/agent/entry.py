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

  * Incomplete range: once the range should be set, fewer than
    or_minutes / bar_minutes completed opening bars (a halt, a thin stock, Yahoo
    lagging) is "no_data" with "opening range incomplete": no range, no trade.

The rule itself lives in `orb_trigger`, a pure function of one session's bars.
check_orb, scanner.scan_orb and timemachine.replay_orb_history all call it, so
the entry timer, the scanner and the history replay cannot disagree.

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


@dataclass(frozen=True)
class OrbTrigger:
    """What `orb_trigger` found on one session's bars."""
    status: str                                  # no_data | waiting | triggered | expired
    reason: str                                  # one of the ORB_* phrases below (or "no bars")
    range_high: float | None = None
    range_low: float | None = None
    range_bars: int = 0
    need_bars: int = 0
    trigger_idx: int | None = None               # position of the trigger bar in the bars passed in
    trigger_start: pd.Timestamp | None = None
    trigger_time: pd.Timestamp | None = None     # the trigger bar's END (close) time
    trigger_price: float | None = None           # its close
    break_idx: int | None = None                 # expired by a close below the range low: that bar
    break_start: pd.Timestamp | None = None
    break_time: pd.Timestamp | None = None
    break_price: float | None = None


ORB_INCOMPLETE = "opening range incomplete"
ORB_LOW_BREAK = "closed below the range low first"
ORB_CUTOFF = "no close above the range high by the latest entry time"
ORB_TRIGGERED = "closed above the range high"
ORB_WAITING = "no close outside the range yet"


def orb_trigger(bars: pd.DataFrame | None, or_minutes: int = 15, bar_minutes: int = 5,
                latest_entry: dt.time = dt.time(14, 30), at: dt.datetime | None = None) -> OrbTrigger:
    """THE opening-range breakout rule on one session's bars (see the module doc).

    `bars`: one IST day's regular-session bars, sorted, one per start time (what
    `session_bars` returns). Only bars complete by `at` count (start + bar_minutes
    <= at); `at=None` treats every bar as complete and the day as over.

      no_data    fewer than or_minutes // bar_minutes completed opening-range bars
                 (ORB_INCOMPLETE), or no bars at all.
      triggered  the first completed bar after the range that closes strictly above
                 the range high and ends no later than `latest_entry`.
      expired    a completed bar closed below the range low before any close above
                 the high (ORB_LOW_BREAK), or no trigger and `at` >= `latest_entry`.
      waiting    none of the above yet.
    """
    if bars is None or len(bars) == 0:
        return OrbTrigger("no_data", "no bars")
    day = bars.index[0].date()
    or_end = dt.datetime.combine(day, OPEN, IST) + dt.timedelta(minutes=or_minutes)
    cutoff = dt.datetime.combine(day, latest_entry, IST)
    clock = at if at is not None else dt.datetime.combine(day, dt.time(23, 59), IST)
    done = completed(bars, clock, bar_minutes)
    need = max(1, or_minutes // bar_minutes)
    in_range = done.index < or_end
    rng = done[in_range]
    if len(rng) < need:
        return OrbTrigger("no_data", ORB_INCOMPLETE, range_bars=len(rng), need_bars=need)
    hi, lo = float(rng["High"].max()), float(rng["Low"].min())
    base = dict(range_high=hi, range_low=lo, range_bars=len(rng), need_bars=need)
    step = pd.Timedelta(minutes=bar_minutes)
    closes = done["Close"].to_numpy(float)
    for k in range(int(in_range.sum()), len(done)):
        start = done.index[k]
        end = start + step
        if end > cutoff:
            break
        close = float(closes[k])
        if close > hi:
            return OrbTrigger("triggered", ORB_TRIGGERED, **base, trigger_idx=k, trigger_start=start,
                              trigger_time=end, trigger_price=close)
        if close < lo:
            return OrbTrigger("expired", ORB_LOW_BREAK, **base, break_idx=k, break_start=start,
                              break_time=end, break_price=close)
    if clock >= cutoff:
        return OrbTrigger("expired", ORB_CUTOFF, **base)
    return OrbTrigger("waiting", ORB_WAITING, **base)


def check_orb(bars: pd.DataFrame, day: dt.date | None = None, at: dt.datetime | None = None,
              or_minutes: int = 15, bar_minutes: int = 5, latest_entry: dt.time = dt.time(14, 30)) -> EntryState:
    """Opening-range breakout status of one stock's intraday bars at `at` (IST)."""
    day, now = resolve_clock(bars, day, at)
    st = EntryState(status="no_data", reason="", day=day, at=now)
    open_dt = dt.datetime.combine(day, OPEN, IST)
    or_end = open_dt + dt.timedelta(minutes=or_minutes)

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
    res = orb_trigger(day_bars, or_minutes, bar_minutes, latest_entry, at=now)
    if res.status == "no_data":
        st.reason = (f"No trade: {ORB_INCOMPLETE} ({res.range_bars} of {res.need_bars} bars closed for "
                     f"{hhmm(OPEN)}-{hhmm(or_end)} IST; halted, thin or Yahoo lagging).")
        return st
    hi, lo = float(res.range_high), float(res.range_low)
    if res.status == "triggered":
        start, end, close = res.trigger_start, res.trigger_time, float(res.trigger_price)
        st.status = "triggered"
        st.trigger_time = end.to_pydatetime()
        st.trigger_price = round(close, 2)
        st.pct_over = round(100.0 * (close / hi - 1.0), 2)
        mins = int((now - st.trigger_time).total_seconds() // 60)
        late = f" That was {mins} minutes ago; a late entry is a different trade." if mins > bar_minutes else ""
        st.reason = (f"Triggered at {hhmm(end)} IST at {rs(close)}: the {hhmm(start)} bar closed above the "
                     f"range high {rs(hi)} (+{st.pct_over:.2f}%).{late} Nothing was placed.")
        return st
    if res.status == "expired" and res.reason == ORB_LOW_BREAK:
        st.status = "expired"
        st.reason = (f"Expired: the {hhmm(res.break_start)} bar closed at {rs(res.break_price)}, below the range "
                     f"low {rs(lo)}, before any close above the high {rs(hi)}. The long setup failed for today.")
        return st
    if res.status == "expired":
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
