"""Offline tests for the chart scanner: daily setups and opening-range breakouts."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

from papertrader.agent import scanner
from papertrader.agent.market import IST
from tests.conftest import make_ohlc

FIX = Path(__file__).parent / "fixtures" / "agent" / "scanner"
DAY = dt.date(2026, 10, 28)


def setups_of(hits, ticker):
    return {h["setup"] for h in hits if h["ticker"] == ticker}


def flat(n=60, price=100.0, wiggle=0.5):
    """Sideways closes: a gentle zig-zag so RSI stays mid-range."""
    return [price + (wiggle if i % 2 else -wiggle) for i in range(n)]


# ---------------------------------------------------------------- daily setups

def test_breakout_needs_volume():
    closes = flat(40) + [104.0]
    df = make_ohlc(closes)
    df.loc[df.index[-1], "Volume"] = 2.0e6                 # 2x the 1e6 average
    hits = scanner.scan_daily({"AAA.NS": df}, setups=["breakout"])
    assert len(hits) == 1
    h = hits[0]
    assert h["setup"] == "breakout" and h["close"] == 104.0
    assert "20-day high" in h["detail"] and "2.0x" in h["detail"] and "Rs" in h["detail"]

    df.loc[df.index[-1], "Volume"] = 1.2e6                 # too thin
    assert scanner.scan_daily({"AAA.NS": df}, setups=["breakout"]) == []


def test_bull_flag():
    run = list(np.linspace(100, 100, 30)) + list(np.linspace(100, 115, 11))[1:]   # +15% in 10 sessions
    closes = run + [115.0, 114.5, 115.2, 114.8, 115.1]
    hits = scanner.scan_daily({"FLAG.NS": make_ohlc(closes)}, setups=["bull_flag"])
    assert [h["setup"] for h in hits] == ["bull_flag"]
    assert "Flag high" in hits[0]["detail"]


def test_oversold_and_bounce():
    closes = flat(30) + list(np.linspace(100, 70, 12))[1:] + [72.0]   # long slide, then an up day
    hits = scanner.scan_daily({"DOWN.NS": make_ohlc(closes)}, setups=["oversold_bounce", "oversold"])
    got = setups_of(hits, "DOWN.NS")
    assert "oversold_bounce" in got
    bounce = next(h for h in hits if h["setup"] == "oversold_bounce")
    assert "RSI(14)" in bounce["detail"] and "closed up" in bounce["detail"]


def test_no_bounce_without_up_close():
    closes = flat(30) + list(np.linspace(100, 70, 12))[1:]
    hits = scanner.scan_daily({"DOWN.NS": make_ohlc(closes)}, setups=["oversold_bounce"])
    assert hits == []


def test_gaps():
    closes = flat(40) + [103.0]
    opens = list(np.concatenate([[closes[0]], closes[:-1]]))
    opens[-1] = 103.5                                      # vs prev close 100.5: +3%
    up = make_ohlc(closes, opens=opens)
    closes_d = flat(40) + [97.0]
    opens_d = list(np.concatenate([[closes_d[0]], closes_d[:-1]]))
    opens_d[-1] = 97.5
    down = make_ohlc(closes_d, opens=opens_d)
    hits = scanner.scan_daily({"UP.NS": up, "DN.NS": down}, setups=["gap_up", "gap_down"])
    assert setups_of(hits, "UP.NS") == {"gap_up"} and setups_of(hits, "DN.NS") == {"gap_down"}
    assert "faded from the open" in next(h for h in hits if h["ticker"] == "UP.NS")["detail"]


def test_near_52w_high():
    closes = list(np.linspace(80, 120, 260)) + [119.0]
    hits = scanner.scan_daily({"HI.NS": make_ohlc(closes)}, setups=["near_52w_high"])
    assert len(hits) == 1 and "52-week high" in hits[0]["detail"]
    far = list(np.linspace(120, 80, 261))
    assert scanner.scan_daily({"LO.NS": make_ohlc(far)}, setups=["near_52w_high"]) == []


def test_sorted_best_first_and_required_keys():
    big = make_ohlc(flat(40) + [110.0])
    big.loc[big.index[-1], "Volume"] = 4e6
    small = make_ohlc(flat(40) + [101.5])
    small.loc[small.index[-1], "Volume"] = 1.6e6
    hits = scanner.scan_daily({"SMALL.NS": small, "BIG.NS": big}, setups=["breakout"])
    assert [h["ticker"] for h in hits] == ["BIG.NS", "SMALL.NS"]
    assert all({"ticker", "setup", "close", "detail", "score"} <= set(h) for h in hits)


def test_bad_data_is_skipped():
    short = make_ohlc([100.0] * 5)
    junk = pd.DataFrame({"Close": [1, 2, 3]})
    nan = make_ohlc(flat(40))
    nan.iloc[-1] = np.nan
    assert scanner.scan_daily({"S.NS": short, "J.NS": junk, "N.NS": None}, setups=None) == []
    scanner.scan_daily({"NAN.NS": nan})                     # must not raise
    assert scanner.scan_daily({}) == []


# ---------------------------------------------------------------- opening-range breakouts

def intraday(closes, highs, lows, day=DAY):
    idx = pd.date_range(dt.datetime.combine(day, dt.time(9, 15), IST), periods=len(closes), freq="5min")
    return pd.DataFrame({"Open": closes, "High": highs, "Low": lows, "Close": closes, "Volume": 1000.0},
                        index=idx)


LONG = intraday([100.2, 100.4, 100.0, 100.5, 102.0, 102.5],
                [101, 100.8, 100.6, 100.9, 102.2, 102.8], [99, 99.3, 99.5, 99.8, 100.4, 101.5])
SHORT = intraday([100.2, 100.4, 100.0, 98.0, 97.5],
                 [101, 100.8, 100.6, 100.1, 98.5], [99, 99.3, 99.5, 97.8, 97.0])
FLAT = intraday([100.2, 100.4, 100.0, 100.5, 100.6],
                [101, 100.8, 100.6, 100.9, 100.9], [99, 99.3, 99.5, 99.8, 99.9])


def test_scan_orb_long_short_and_order():
    hits = scanner.scan_orb({"S.NS": SHORT, "L.NS": LONG, "F.NS": FLAT}, day=DAY, at=dt.time(9, 45))
    assert [(h["ticker"], h["setup"]) for h in hits] == [("L.NS", "orb_long"), ("S.NS", "orb_short")]
    long = hits[0]
    assert long["time"] == "09:40" and long["range_high"] == 101.0 and long["close"] == 102.0
    assert long["pct_beyond"] == round(100 * (102 / 101 - 1), 2)
    short = hits[1]
    assert short["time"] == "09:35" and "information only" in short["detail"]


def test_scan_orb_only_completed_bars():
    # At 09:37 the 09:35 bar (close 102) is still forming: no long hit yet.
    hits = scanner.scan_orb({"L.NS": LONG}, day=DAY, at=dt.time(9, 37))
    assert hits == []
    assert scanner.scan_orb({"L.NS": LONG}, day=DAY, at=dt.time(9, 25)) == []   # range not set


def test_scan_orb_missing_range_bar_skipped():
    gappy = LONG.drop(LONG.index[1])
    assert scanner.scan_orb({"G.NS": gappy}, day=DAY, at=dt.time(10, 0)) == []


def test_scan_orb_default_day_is_latest_session():
    hits = scanner.scan_orb({"L.NS": LONG, "E.NS": pd.DataFrame()})
    assert len(hits) == 1 and hits[0]["date"] == DAY.isoformat()


def test_scan_orb_real_fixture():
    b = pd.read_csv(FIX / "INFY-5m-2026-10-28.csv", index_col=0)
    b.index = pd.to_datetime(b.index)
    hits = scanner.scan_orb({"INFY.NS": b}, day=DAY, at=dt.time(11, 0))
    assert len(hits) == 1 and hits[0]["setup"] == "orb_short" and hits[0]["time"] == "10:25"


def test_describe_hits():
    assert scanner.describe_hits([]) == "No charts matched a setup rule."
    text = scanner.describe_hits(scanner.scan_orb({"L.NS": LONG}, day=DAY, at=dt.time(10, 0)))
    assert "orb_long" in text and "not a buy" in text and "IST" in text
