"""NSE market clock: sessions, holidays, open / closed, next open. All times IST.

Equity cash market (normal days):
  pre-open   09:00-09:08 (order entry), matching till 09:15
  continuous 09:15-15:30
  closing session 15:40-16:00 (at the closing price; ignored here)
Intraday (MIS) positions are squared off by most brokers from about 15:15-15:25,
so day-trade plans default to being flat by FLAT_BY (15:15).

Special sessions (Muhurat trading on Diwali) are not modelled: that day counts as
a holiday for planning.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

PRE_OPEN = dt.time(9, 0)
OPEN = dt.time(9, 15)
CLOSE = dt.time(15, 30)
FLAT_BY = dt.time(15, 15)

# NSE equity-segment trading holidays (weekday closures only), from NSE's circulars.
# Filled and verified by the market-data desk; update every December from
# https://www.nseindia.com/resources/exchange-communication-holidays
#
# 2026: NSE's own list (www.nseindia.com/api/holiday-master?type=trading, segment
# "CM", read 2026-10-29), cross-checked with Bajaj Broking / Cleartax. Weekend
# holidays (Mahashivratri Sun 15 Feb, Id-Ul-Fitr Sat 21 Mar, Independence Day
# Sat 15 Aug, Diwali Laxmi Pujan Sun 8 Nov) are left out: weekends are closed anyway.
# Muhurat trading 2026 is on Sun 8 Nov, a weekend, so it is already "closed" here.
# 2027: not yet published by NSE as of 2026-10-29 (it usually appears in December).
# Until it is added, every 2027 weekday counts as a trading day.
HOLIDAYS: dict[dt.date, str] = {
    dt.date(2026, 1, 15): "Municipal Corporation Election - Maharashtra",
    dt.date(2026, 1, 26): "Republic Day",
    dt.date(2026, 3, 3): "Holi",
    dt.date(2026, 3, 26): "Shri Ram Navami",
    dt.date(2026, 3, 31): "Shri Mahavir Jayanti",
    dt.date(2026, 4, 3): "Good Friday",
    dt.date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    dt.date(2026, 5, 1): "Maharashtra Day",
    dt.date(2026, 5, 28): "Bakri Id",
    dt.date(2026, 6, 26): "Muharram",
    dt.date(2026, 9, 14): "Ganesh Chaturthi",
    dt.date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    dt.date(2026, 10, 20): "Dussehra",
    dt.date(2026, 11, 10): "Diwali Balipratipada",
    dt.date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
    dt.date(2026, 12, 25): "Christmas",
}


def holidays_known(year: int) -> bool:
    """True when HOLIDAYS lists the given year (NSE publishes the next year's list in December)."""
    return any(d.year == year for d in HOLIDAYS)


def now_ist(now: dt.datetime | None = None) -> dt.datetime:
    if now is None:
        return dt.datetime.now(IST)
    return now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def holiday_name(d: dt.date) -> str | None:
    if d.weekday() >= 5:
        return "weekend"
    return HOLIDAYS.get(d)


def next_trading_day(d: dt.date, include_today: bool = False) -> dt.date:
    day = d if include_today else d + dt.timedelta(days=1)
    while not is_trading_day(day):
        day += dt.timedelta(days=1)
    return day


def previous_trading_day(d: dt.date) -> dt.date:
    day = d - dt.timedelta(days=1)
    while not is_trading_day(day):
        day -= dt.timedelta(days=1)
    return day


def trading_days_between(start: dt.date, end: dt.date) -> int:
    """Trading days in (start, end]."""
    n, day = 0, start
    while day < end:
        day += dt.timedelta(days=1)
        n += is_trading_day(day)
    return n


@dataclass(frozen=True)
class MarketStatus:
    now: dt.datetime
    phase: str            # closed | pre_open | open | post_close
    is_open: bool
    next_open: dt.datetime
    reason: str

    def describe(self) -> str:
        return f"NSE {self.phase.replace('_', '-')} at {self.now:%a %d %b %H:%M} IST. {self.reason}"


def market_status(now: dt.datetime | None = None) -> MarketStatus:
    t = now_ist(now)
    d = t.date()

    def opening(day: dt.date) -> dt.datetime:
        return dt.datetime.combine(day, OPEN, IST)

    if not is_trading_day(d):
        nxt = opening(next_trading_day(d))
        return MarketStatus(t, "closed", False, nxt, f"Closed ({holiday_name(d)}). Next open {nxt:%a %d %b %H:%M}.")
    clock = t.time()
    if clock < PRE_OPEN:
        return MarketStatus(t, "closed", False, opening(d), f"Opens today at {OPEN:%H:%M}.")
    if clock < OPEN:
        return MarketStatus(t, "pre_open", False, opening(d), f"Pre-open; continuous trading from {OPEN:%H:%M}.")
    if clock < CLOSE:
        return MarketStatus(t, "open", True, opening(next_trading_day(d)), f"Open till {CLOSE:%H:%M}.")
    nxt = opening(next_trading_day(d))
    return MarketStatus(t, "post_close", False, nxt, f"Closed for the day. Next open {nxt:%a %d %b %H:%M}.")
