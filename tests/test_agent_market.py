"""Offline tests: NSE clock and holidays, universe loading, intraday cleaning and cache."""
from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

import pandas as pd
import pytest

from papertrader.agent import intraday, market, universe
from papertrader.agent.market import IST

FIX = Path(__file__).parent / "fixtures" / "agent"


def ist(y, mo, d, h=0, mi=0):
    return dt.datetime(y, mo, d, h, mi, tzinfo=IST)


# ---------------------------------------------------------------- holidays / clock

def test_holidays_are_weekdays_and_known_dates():
    assert all(d.weekday() < 5 for d in market.HOLIDAYS)
    assert market.HOLIDAYS[dt.date(2026, 1, 26)] == "Republic Day"
    assert dt.date(2026, 11, 10) in market.HOLIDAYS          # Diwali Balipratipada
    assert dt.date(2026, 1, 15) in market.HOLIDAYS           # Maharashtra civic polls
    assert len([d for d in market.HOLIDAYS if d.year == 2026]) == 16


def test_is_trading_day():
    assert market.is_trading_day(dt.date(2026, 10, 29))      # Thursday
    assert not market.is_trading_day(dt.date(2026, 10, 20))  # Dussehra
    assert not market.is_trading_day(dt.date(2026, 10, 31))  # Saturday
    assert market.holiday_name(dt.date(2026, 10, 20)) == "Dussehra"


def test_next_and_previous_trading_day_skip_holidays():
    # Mon 9 Nov -> Tue 10 Nov is Diwali Balipratipada -> Wed 11 Nov
    assert market.next_trading_day(dt.date(2026, 11, 9)) == dt.date(2026, 11, 11)
    assert market.previous_trading_day(dt.date(2026, 10, 21)) == dt.date(2026, 10, 19)


@pytest.mark.parametrize("when,phase,is_open", [
    (ist(2026, 10, 29, 8, 30), "closed", False),
    (ist(2026, 10, 29, 9, 5), "pre_open", False),
    (ist(2026, 10, 29, 9, 15), "open", True),
    (ist(2026, 10, 29, 15, 29), "open", True),
    (ist(2026, 10, 29, 15, 30), "post_close", False),
    (ist(2026, 10, 31, 11, 0), "closed", False),   # Saturday
    (ist(2026, 10, 20, 11, 0), "closed", False),   # Dussehra
])
def test_market_status_phases(when, phase, is_open):
    s = market.market_status(when)
    assert (s.phase, s.is_open) == (phase, is_open)


def test_market_status_holiday_next_open():
    s = market.market_status(ist(2026, 11, 10, 10, 0))
    assert s.next_open == ist(2026, 11, 11, 9, 15)
    assert "Diwali" in s.reason


def test_market_status_converts_utc():
    s = market.market_status(dt.datetime(2026, 10, 29, 4, 0, tzinfo=dt.timezone.utc))  # 09:30 IST
    assert s.phase == "open"


# ---------------------------------------------------------------- universe

def fixture_bytes() -> bytes:
    return (FIX / "universe" / "ind_nifty50list.csv").read_bytes()


def make_old(path: Path, days: int = 30) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_parse_constituents_real_file():
    tickers = universe.parse_constituents(fixture_bytes().decode())
    assert len(tickers) == 50
    assert "INFY.NS" in tickers and "M&M.NS" in tickers
    assert all(t.endswith(".NS") for t in tickers)


def test_load_universe_downloads_and_caches(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        return fixture_bytes()

    t1 = universe.load_universe("nifty50", fetch=fetch, cache_dir=tmp_path)
    assert calls == ["https://niftyindices.com/IndexConstituent/ind_nifty50list.csv"]
    assert (tmp_path / "nifty50.csv").exists()
    t2 = universe.load_universe("NIFTY50", fetch=fetch, cache_dir=tmp_path)   # fresh cache: no download
    assert t1 == t2 and len(calls) == 1


def test_load_universe_refreshes_old_cache(tmp_path):
    (tmp_path / "nifty50.csv").write_text("Company Name,Industry,Symbol,Series,ISIN Code\nX,Y,OLDCO,EQ,Z\n")
    make_old(tmp_path / "nifty50.csv")
    assert len(universe.load_universe("nifty50", fetch=lambda u: fixture_bytes(), cache_dir=tmp_path)) == 50


def test_load_universe_stale_cache_on_failure(tmp_path):
    (tmp_path / "nifty50.csv").write_bytes(fixture_bytes())
    make_old(tmp_path / "nifty50.csv")

    def broken(url):
        raise OSError("network down")

    assert len(universe.load_universe("nifty50", fetch=broken, cache_dir=tmp_path)) == 50


def test_load_universe_garbage_download_uses_cache(tmp_path):
    (tmp_path / "nifty50.csv").write_bytes(fixture_bytes())
    make_old(tmp_path / "nifty50.csv")
    got = universe.load_universe("nifty50", fetch=lambda u: b"<html>maintenance</html>", cache_dir=tmp_path)
    assert len(got) == 50


def test_load_universe_nothing_at_all_raises(tmp_path):
    def broken(url):
        raise OSError("network down")

    with pytest.raises(universe.UniverseError, match="no cache"):
        universe.load_universe("nifty100", fetch=broken, cache_dir=tmp_path)
    with pytest.raises(universe.UniverseError, match="unknown universe"):
        universe.load_universe("sensex", fetch=broken, cache_dir=tmp_path)


# ---------------------------------------------------------------- intraday

def raw_5m(day=dt.date(2026, 10, 28)):
    """A raw Yahoo-like download: pre-open print, 75 session bars, a live tick, a 15:30 print."""
    idx = [dt.datetime.combine(day, dt.time(9, 0), IST)]
    idx += list(pd.date_range(dt.datetime.combine(day, dt.time(9, 15), IST), periods=75, freq="5min"))
    idx += [dt.datetime.combine(day, dt.time(10, 37, 42), IST), dt.datetime.combine(day, dt.time(15, 30), IST)]
    n = len(idx)
    return pd.DataFrame({"Open": [100.0] * n, "High": [101.0] * n, "Low": [99.0] * n, "Close": [100.5] * n,
                         "Volume": [1000] * n, "Adj Close": [100.5] * n}, index=pd.DatetimeIndex(idx))


def test_clean_intraday_keeps_session_grid_only():
    df = intraday.clean_intraday(raw_5m())
    assert len(df) == 75
    assert str(df.index.tz) == "Asia/Kolkata"
    assert df.index[0].time() == dt.time(9, 15) and df.index[-1].time() == dt.time(15, 25)
    assert list(df.columns) == intraday.COLUMNS


def test_clean_intraday_bad_input_is_empty():
    assert intraday.clean_intraday(None).empty
    assert intraday.clean_intraday(pd.DataFrame({"Close": [1.0]})).empty


def test_load_intraday_cache_and_fallback(tmp_path):
    calls = []

    def dl(ticker, interval, days):
        calls.append((ticker, interval, days))
        return raw_5m()

    now = ist(2026, 10, 28, 18, 0)   # after the close
    df = intraday.load_intraday("INFY.NS", downloader=dl, cache_dir=tmp_path, now=now)
    assert len(df) == 75 and calls == [("INFY.NS", "5m", 59)]
    intraday.load_intraday("INFY.NS", downloader=dl, cache_dir=tmp_path, now=now)
    assert len(calls) == 1                                   # fetched after the close: still fresh

    def broken(*a):
        raise RuntimeError("rate limited")

    later = ist(2026, 10, 29, 11, 0)                         # market open next day: cache stale
    df2 = intraday.load_intraday("INFY.NS", downloader=broken, cache_dir=tmp_path, now=later)
    assert len(df2) == 75                                    # stale cache used
    assert intraday.load_intraday("TCS.NS", downloader=broken, cache_dir=tmp_path, now=later).empty


def ists(h, mi, sec, day=dt.date(2026, 10, 28)):
    return dt.datetime.combine(day, dt.time(h, mi, sec), IST)


def test_load_intraday_fresh_only_after_latest_bar_boundary(tmp_path):
    calls = []

    def dl(ticker, interval, days):
        calls.append(1)
        return raw_5m()

    intraday.load_intraday("A.NS", downloader=dl, cache_dir=tmp_path, now=ists(11, 0, 45))
    intraday.load_intraday("A.NS", downloader=dl, cache_dir=tmp_path, now=ists(11, 3, 0))
    assert len(calls) == 1                                   # same bar boundary: cache reused
    # Regression: the 11:00 bar closed at 11:05. A cycle at 11:05:40 must not reuse the
    # 11:00:45 download (the old 5-minute TTL did: 4m55s old), or it misses that bar.
    intraday.load_intraday("A.NS", downloader=dl, cache_dir=tmp_path, now=ists(11, 5, 40))
    assert len(calls) == 2
    intraday.load_intraday("A.NS", downloader=dl, cache_dir=tmp_path, now=ists(11, 6, 0))
    assert len(calls) == 2
    # Fetched inside the 30 s grace after a boundary: not trusted for that boundary.
    intraday.load_intraday("B.NS", downloader=dl, cache_dir=tmp_path, now=ists(11, 10, 10))
    intraday.load_intraday("B.NS", downloader=dl, cache_dir=tmp_path, now=ists(11, 10, 50))
    assert len(calls) == 4


def test_load_intraday_refetches_when_just_closed_bar_missing(tmp_path):
    calls = []

    def lagging(ticker, interval, days):                     # Yahoo has not published the 10:55 bar yet
        calls.append(1)
        raw = raw_5m()
        return raw[raw.index < pd.Timestamp(ists(10, 55, 0))]

    intraday.load_intraday("A.NS", downloader=lagging, cache_dir=tmp_path, now=ists(11, 0, 40))
    intraday.load_intraday("A.NS", downloader=lagging, cache_dir=tmp_path, now=ists(11, 1, 20))
    assert len(calls) == 1                                   # within the 60 s retry TTL
    intraday.load_intraday("A.NS", downloader=lagging, cache_dir=tmp_path, now=ists(11, 2, 0))
    assert len(calls) == 2                                   # still missing the 10:55 bar: fetch again


def test_latest_boundary():
    assert intraday.latest_boundary(ists(11, 7, 59)) == ists(11, 5, 0)
    assert intraday.latest_boundary(ists(9, 15, 0)) == ists(9, 15, 0)
    assert intraday.latest_boundary(ists(9, 16, 30), "1m") == ists(9, 16, 0)


def test_load_intraday_clamps_days_and_drops_future(tmp_path):
    seen = {}

    def dl(ticker, interval, days):
        seen["days"] = days
        return raw_5m()

    df = intraday.load_intraday("X.NS", interval="5m", days=200, downloader=dl, cache_dir=tmp_path,
                                now=ist(2026, 10, 28, 10, 0))
    assert seen["days"] == 59
    assert df.index[-1] == ist(2026, 10, 28, 10, 0)          # bars starting after `now` dropped
    intraday.load_intraday("Y.NS", interval="1m", days=30, downloader=dl, cache_dir=tmp_path,
                           now=ist(2026, 10, 28, 18, 0))
    assert seen["days"] == 7


def test_bars_on():
    a = intraday.clean_intraday(raw_5m(dt.date(2026, 10, 27)))
    b = intraday.clean_intraday(raw_5m(dt.date(2026, 10, 28)))
    both = pd.concat([a, b])
    assert len(intraday.bars_on(both, dt.date(2026, 10, 28))) == 75
    assert intraday.bars_on(both, dt.date(2026, 10, 29)).empty
