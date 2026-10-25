import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd

from papertrader.data import clean, drop_incomplete_bar, load_prices
from tests.conftest import make_ohlc

IST = ZoneInfo("Asia/Kolkata")


def test_clean_strips_timezone_and_bad_rows():
    df = make_ohlc([100, 101, 102])
    df.index = df.index.tz_localize(IST)
    df.iloc[1, 0] = float("nan")
    out = clean(df)
    assert out.index.tz is None and len(out) == 2


def test_incomplete_bar_dropped_only_before_close():
    df = make_ohlc([100, 101], start="2024-03-04")  # Mon, Tue
    tue = dt.date(2024, 3, 5)
    before = dt.datetime.combine(tue, dt.time(14, 0), IST)
    after = dt.datetime.combine(tue, dt.time(16, 30), IST)
    assert len(drop_incomplete_bar(df, before, dt.time(15, 45))) == 1
    assert len(drop_incomplete_bar(df, after, dt.time(15, 45))) == 2


def test_load_prices_uses_cache_and_falls_back_when_download_fails(cfg):
    calls = []

    def fake(ticker, start):
        calls.append(ticker)
        return make_ohlc([100, 101, 102], start="2024-03-04")

    now = dt.datetime(2024, 3, 8, 17, 0, tzinfo=IST)
    prices, _ = load_prices(["AAA.NS"], cfg.data, now=now, downloader=fake)
    prices2, _ = load_prices(["AAA.NS"], cfg.data, now=now + dt.timedelta(hours=1), downloader=fake)
    assert calls == ["AAA.NS"]  # second call served from cache
    pd.testing.assert_frame_equal(prices["AAA.NS"], prices2["AAA.NS"], check_freq=False)

    def broken(ticker, start):
        raise ConnectionError("offline")

    later = now + dt.timedelta(days=2)
    prices3, warnings = load_prices(["AAA.NS"], cfg.data, now=later, downloader=broken)
    assert len(prices3["AAA.NS"]) == 3 and "cached" in warnings[0]
