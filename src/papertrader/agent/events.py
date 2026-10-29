"""Events desk: when are the next results, and is a planned trade too close to them?

Method (from the US trader-analyst / trader-news skills): a stop does not protect
against an overnight gap, and quarterly results are the most common cause of one.
So before a trade we look up the company's announced board meetings and block
trades that sit within a couple of trading days of a results meeting.

Sources, in order:
  1. NSE board meetings (the company's own intimation to the exchange):
     https://www.nseindia.com/api/corporate-board-meetings?index=equities&symbol=<SYM>
  2. NSE event calendar (same data, different list):
     https://www.nseindia.com/api/event-calendar?index=equities&symbol=<SYM>
  3. Yahoo Finance's earnings date (yfinance Ticker.calendar) when NSE gave no
     upcoming results meeting. Yahoo's date can be an estimate; the summary says so.
NSE's bot protection often answers 401/403 to scripts; after the first such
failure the second NSE list is not tried (one request per source, no hammering).

Finding (desk "events"):
  BLOCK  a results meeting within [on - days_after, on + days_before] trading days;
  INFO   the next results date (or "none announced");
  UNKNOWN no source answered.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from .http import Fetch, FetchError, NSESession, get_json, url_with
from .market import now_ist, trading_days_between
from .types import BLOCK, INFO, Finding, symbol, unknown, yahoo

log = logging.getLogger(__name__)

DESK = "events"
NSE_BOARD_MEETINGS = "https://www.nseindia.com/api/corporate-board-meetings"
NSE_EVENT_CALENDAR = "https://www.nseindia.com/api/event-calendar"
LOOKBACK_DAYS = 10   # keep recent past meetings too: the day after results still gaps

_RESULTS_RX = re.compile(r"financial results?|\bresults?\b|\bearnings\b|quarterly|audited|unaudited", re.I)

CalendarLoader = Callable[[str], Any]


@dataclass(frozen=True)
class Event:
    date: dt.date
    purpose: str
    source: str

    @property
    def is_results(self) -> bool:
        return bool(_RESULTS_RX.search(self.purpose))

    def to_dict(self) -> dict[str, str]:
        return {"date": self.date.isoformat(), "purpose": self.purpose, "source": self.source}


def _parse_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d-%b-%Y %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _rows(doc: Any) -> list[dict]:
    rows = doc.get("data", []) if isinstance(doc, dict) else (doc or [])
    return [r for r in rows if isinstance(r, dict)]


def parse_board_meetings(doc: Any) -> list[Event]:
    out = []
    for r in _rows(doc):
        d = _parse_date(r.get("bm_date") or r.get("meetingDate") or r.get("date"))
        purpose = " - ".join(p for p in (str(r.get("bm_purpose") or r.get("purpose") or "").strip(),
                                         str(r.get("bm_desc") or "").strip()) if p)
        if d:
            out.append(Event(d, purpose or "Board meeting", "NSE board meetings"))
    return out


def parse_event_calendar(doc: Any) -> list[Event]:
    out = []
    for r in _rows(doc):
        d = _parse_date(r.get("date") or r.get("bm_date"))
        purpose = " - ".join(p for p in (str(r.get("purpose") or "").strip(),
                                         str(r.get("bm_desc") or "").strip()) if p)
        if d:
            out.append(Event(d, purpose or "Corporate event", "NSE event calendar"))
    return out


def parse_yf_calendar(cal: Any) -> list[Event]:
    """yfinance Ticker.calendar (a dict in recent versions) -> earnings events."""
    if cal is None:
        return []
    if hasattr(cal, "to_dict") and not isinstance(cal, dict):   # old yfinance: a DataFrame
        try:
            cal = {k: list(v.values()) for k, v in cal.to_dict().items()}
        except Exception:
            return []
    dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
    if dates is None:
        return []
    if not isinstance(dates, (list, tuple)):
        dates = [dates]
    out = []
    for i, v in enumerate(dates[:1]):   # a 2-date list is Yahoo's estimate window; use its start
        d = _parse_date(v)
        if d:
            note = "Financial Results (Yahoo earnings date, may be an estimate)"
            out.append(Event(d, note, "yfinance calendar"))
    return out


def _default_calendar(ticker: str) -> Any:
    import yfinance as yf
    return yf.Ticker(yahoo(ticker)).calendar


def upcoming_events(ticker: str, fetch: Fetch | None = None, today: dt.date | None = None,
                    calendar_loader: CalendarLoader | None = None) -> list[Event]:
    """Corporate events from `today - LOOKBACK_DAYS` on, soonest first.

    Raises FetchError when every source failed; [] means sources answered with nothing.
    """
    sym = symbol(ticker)
    day = today or now_ist().date()
    nse = fetch or NSESession()
    events: list[Event] = []
    errors: list[str] = []
    nse_ok = False
    try:
        events += parse_board_meetings(get_json(url_with(NSE_BOARD_MEETINGS, index="equities", symbol=sym), nse))
        nse_ok = True
    except Exception as exc:
        errors.append(f"NSE board meetings: {exc}")
    if nse_ok:
        try:
            events += parse_event_calendar(get_json(url_with(NSE_EVENT_CALENDAR, index="equities", symbol=sym), nse))
        except Exception as exc:
            errors.append(f"NSE event calendar: {exc}")
    have_results = any(e.is_results and e.date >= day for e in events)
    yf_ok = False
    if not have_results:
        try:
            events += parse_yf_calendar((calendar_loader or _default_calendar)(sym))
            yf_ok = True
        except Exception as exc:
            errors.append(f"yfinance calendar: {exc}")
    if not nse_ok and not yf_ok:
        raise FetchError("; ".join(errors))
    for e in errors:
        log.info("events %s: %s", sym, e)
    start = day - dt.timedelta(days=LOOKBACK_DAYS)
    seen: set[tuple[dt.date, bool]] = set()
    out = []
    for e in sorted(events, key=lambda e: (e.date, e.source != "NSE board meetings")):
        key = (e.date, e.is_results)
        if e.date < start or key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _distance(on: dt.date, d: dt.date) -> int:
    """Signed trading-day distance: + if d is after `on`, - if before, 0 same day."""
    if d >= on:
        return trading_days_between(on, d)
    return -trading_days_between(d, on)


def blackout_check(ticker: str, on: dt.date, days_before: int = 2, days_after: int = 1,
                   fetch: Fetch | None = None, events: list[Event] | None = None,
                   calendar_loader: CalendarLoader | None = None) -> Finding:
    """BLOCK a trade on `on` that is within `days_before` trading days ahead of results,
    or within `days_after` trading days after them. Never raises."""
    sym = symbol(ticker)
    try:
        evs = events if events is not None else upcoming_events(sym, fetch=fetch, today=on,
                                                                calendar_loader=calendar_loader)
    except Exception as exc:
        return unknown(DESK, str(exc)[:200])
    try:
        results = [e for e in evs if e.is_results]
        data: dict[str, Any] = {"events": [e.to_dict() for e in evs[:10]], "on": on.isoformat(),
                                "days_before": days_before, "days_after": days_after}
        for e in results:
            gap = _distance(on, e.date)
            if -days_after <= gap <= days_before:
                when = ("today" if gap == 0 else f"in {gap} trading day{'s' if gap != 1 else ''}" if gap > 0
                        else f"{-gap} trading day{'s' if gap != -1 else ''} ago")
                data["blocking_event"] = e.to_dict()
                what = "results board meeting" if e.source.startswith("NSE") else "results expected"
                return Finding(DESK, BLOCK,
                               f"{sym}: {what} on {e.date:%a %d %b %Y} ({when}, {e.source}); "
                               f"no new trades from {days_before} trading days before to {days_after} after "
                               "results: a stop cannot protect against the gap.", data)
        nxt = next((e for e in results if e.date >= on), None)
        if nxt is None:
            others = [e for e in evs if e.date >= on]
            extra = f" Next event: {others[0].purpose[:80]} on {others[0].date:%d %b %Y}." if others else ""
            return Finding(DESK, INFO, f"{sym}: no upcoming results date announced.{extra}", data)
        gap = _distance(on, nxt.date)
        data["next_results"] = nxt.to_dict()
        return Finding(DESK, INFO, f"{sym}: next results on {nxt.date:%a %d %b %Y}, {gap} trading days away "
                                   f"({nxt.source}).", data)
    except Exception as exc:
        return unknown(DESK, f"bad event data: {exc}"[:200])
