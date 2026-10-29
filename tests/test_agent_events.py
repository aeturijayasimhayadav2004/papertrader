from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from papertrader.agent.events import Event, blackout_check, parse_yf_calendar, upcoming_events
from papertrader.agent.http import FetchError

FIX = Path(__file__).parent / "fixtures" / "agent" / "events"
NSE = {"corporate-board-meetings": "board_meetings_reliance.json", "event-calendar": "event_calendar_reliance.json"}


def router(routes: dict[str, str]):
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        for part, name in routes.items():
            if part in url:
                return (FIX / name).read_bytes()
        raise FetchError(f"{url}: HTTP Error 403: Forbidden")
    fetch.calls = calls
    return fetch


def no_yahoo(_t):
    raise AssertionError("yfinance should not be needed")


def test_upcoming_events_from_nse_dedupes_and_sorts():
    evs = upcoming_events("RELIANCE.NS", fetch=router(NSE), today=dt.date(2026, 10, 29), calendar_loader=no_yahoo)
    assert [e.date for e in evs] == [dt.date(2026, 10, 30), dt.date(2026, 12, 18)]   # July meeting too old
    assert evs[0].is_results and evs[0].source == "NSE board meetings"
    assert not evs[1].is_results


def test_block_day_before_results():
    f = blackout_check("RELIANCE.NS", dt.date(2026, 10, 29), fetch=router(NSE), calendar_loader=no_yahoo)
    assert f.status == "block"
    assert "30 Oct 2026" in f.summary and "in 1 trading day" in f.summary


def test_block_window_edges_in_trading_days():
    ev = [Event(dt.date(2026, 10, 30), "Financial Results", "test")]       # a Friday

    def on(d):
        return blackout_check("X", d, events=ev).status
    assert on(dt.date(2026, 10, 28)) == "block"      # 2 trading days before
    assert on(dt.date(2026, 10, 27)) == "info"       # 3 trading days before
    assert on(dt.date(2026, 10, 30)) == "block"      # results day
    assert on(dt.date(2026, 11, 2)) == "block"       # Monday: 1 trading day after (weekend skipped)
    assert on(dt.date(2026, 11, 3)) == "info"        # 2 trading days after
    assert blackout_check("X", dt.date(2026, 11, 3), days_after=2, events=ev).status == "block"


def test_non_results_meeting_does_not_block():
    ev = [Event(dt.date(2026, 10, 30), "Fund Raising - issue of NCDs", "test")]
    f = blackout_check("X", dt.date(2026, 10, 29), events=ev)
    assert f.status == "info" and "no upcoming results" in f.summary


def test_info_names_next_results_date():
    f = blackout_check("RELIANCE.NS", dt.date(2026, 10, 20), fetch=router(NSE), calendar_loader=no_yahoo)
    assert f.status == "info" and "next results on Fri 30 Oct 2026" in f.summary
    assert f.data["next_results"]["date"] == "2026-10-30"


def test_yfinance_fallback_when_nse_refuses():
    cal = {"Ex-Dividend Date": dt.date(2026, 11, 3), "Earnings Date": [dt.date(2027, 1, 13)]}
    fetch = router({})
    f = blackout_check("INFY.NS", dt.date(2026, 10, 29), fetch=fetch, calendar_loader=lambda t: cal)
    assert f.status == "info" and "13 Jan 2027" in f.summary and "yfinance" in f.summary
    assert len(fetch.calls) == 1                       # second NSE list not tried after a refusal
    f2 = blackout_check("INFY.NS", dt.date(2027, 1, 12), fetch=fetch, calendar_loader=lambda t: cal)
    assert f2.status == "block"


def test_yf_calendar_estimate_window_uses_first_date():
    evs = parse_yf_calendar({"Earnings Date": [dt.date(2027, 1, 13), dt.date(2027, 1, 17)]})
    assert [e.date for e in evs] == [dt.date(2027, 1, 13)]
    assert parse_yf_calendar({}) == [] and parse_yf_calendar(None) == []


def test_all_sources_fail_is_unknown():
    def boom(_t):
        raise RuntimeError("yahoo down")
    with pytest.raises(FetchError):
        upcoming_events("X", fetch=router({}), today=dt.date(2026, 10, 29), calendar_loader=boom)
    f = blackout_check("X", dt.date(2026, 10, 29), fetch=router({}), calendar_loader=boom)
    assert f.status == "unknown" and f.desk == "events"
