"""Offline tests for the opening-range entry timer (entry.check_orb)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from papertrader.agent.entry import check_orb
from papertrader.agent.market import IST

DAY = dt.date(2026, 10, 28)          # a Wednesday, a normal session
FIX = Path(__file__).parent / "fixtures" / "agent" / "scanner"


def at(h, m, day=DAY):
    return dt.datetime.combine(day, dt.time(h, m), IST)


def bars(closes, highs=None, lows=None, day=DAY, step=5, vol=1000.0):
    """5-minute bars from 09:15. Range bars default to high 101 / low 99 around 100."""
    idx = pd.date_range(dt.datetime.combine(day, dt.time(9, 15), IST), periods=len(closes), freq=f"{step}min")
    highs = highs or [max(c, 100.0) + 0.5 for c in closes]
    lows = lows or [min(c, 100.0) - 0.5 for c in closes]
    return pd.DataFrame({"Open": closes, "High": highs, "Low": lows, "Close": closes,
                         "Volume": [vol] * len(closes)}, index=idx)


# Range bars 09:15/09:20/09:25: high 101, low 99.  Bar 3 (09:30) 100.5, bar 4 (09:35) 102 (> 101).
RANGE = dict(highs=[101, 100.8, 100.6], lows=[99, 99.3, 99.5])


def breakout_day():
    closes = [100.2, 100.4, 100.0, 100.5, 102.0, 102.5]
    highs = RANGE["highs"] + [100.9, 102.2, 102.8]
    lows = RANGE["lows"] + [99.8, 100.4, 101.5]
    return bars(closes, highs, lows)


def test_not_open_yet():
    s = check_orb(breakout_day(), at=at(9, 0))
    assert s.status == "forming_range" and "09:15" in s.reason


def test_forming_range():
    s = check_orb(breakout_day(), at=at(9, 22))
    assert s.status == "forming_range"
    assert s.range_bars == 1 and "1 of 3" in s.reason and "09:30" in s.reason


def test_waiting_with_forming_bar_not_counted():
    # 09:37: the 09:35 bar (close 102 > 101) is still forming, so it must not trigger.
    s = check_orb(breakout_day(), at=at(9, 37))
    assert s.status == "waiting"
    assert (s.range_high, s.range_low) == (101.0, 99.0)
    assert s.last_close == 100.5
    assert "09:35 bar is still forming" in s.reason and "does not count" in s.reason


def test_triggered_on_completed_close():
    s = check_orb(breakout_day(), at=at(9, 40))
    assert s.status == "triggered"
    assert s.trigger_time == at(9, 40) and s.trigger_price == 102.0
    assert s.pct_over == round(100 * (102 / 101 - 1), 2)
    assert "09:40 IST" in s.reason and "Rs 102.00" in s.reason and "Nothing was placed" in s.reason


def test_wick_above_high_is_not_a_trigger():
    closes = [100.2, 100.4, 100.0, 100.9, 100.8]
    highs = RANGE["highs"] + [103.0, 101.5]          # wicks well above 101, closes below
    lows = RANGE["lows"] + [99.9, 99.9]
    s = check_orb(bars(closes, highs, lows), at=at(10, 0))
    assert s.status == "waiting"


def test_close_exactly_at_high_is_not_above():
    closes = [100.2, 100.4, 100.0, 101.0]
    s = check_orb(bars(closes, RANGE["highs"] + [101.0], RANGE["lows"] + [100.0]), at=at(9, 40))
    assert s.status == "waiting"


def test_expired_after_latest_entry():
    closes = [100.2, 100.4, 100.0] + [100.5] * 60      # never closes above 101
    s = check_orb(bars(closes), at=at(14, 45))
    assert s.status == "expired" and "14:30" in s.reason


def test_breakout_after_latest_entry_does_not_count():
    closes = [100.2, 100.4, 100.0] + [100.5] * 62 + [103.0]   # bar 65 starts 14:40
    s = check_orb(bars(closes), at=at(15, 0))
    assert s.status == "expired"


def test_expired_when_range_low_breaks_first():
    closes = [100.2, 100.4, 100.0, 98.5, 102.0]          # 09:30 bar closes under 99, then a breakout
    highs = RANGE["highs"] + [100.0, 102.5]
    lows = RANGE["lows"] + [98.0, 98.4]
    s = check_orb(bars(closes, highs, lows), at=at(10, 0))
    assert s.status == "expired" and "below the range low" in s.reason


def test_no_data_cases():
    empty = bars([])
    assert check_orb(empty, day=DAY, at=at(11, 0)).status == "no_data"
    holiday = dt.date(2026, 10, 20)
    s = check_orb(empty, day=holiday, at=at(11, 0, holiday))
    assert s.status == "no_data" and "Dussehra" in s.reason
    assert check_orb(None, day=DAY, at=at(11, 0)).status == "no_data"
    assert check_orb(pd.DataFrame({"x": [1]}), day=DAY, at=at(11, 0)).status == "no_data"


def test_past_day_without_clock_uses_all_bars():
    s = check_orb(breakout_day())          # DAY is in the past: judged at the end of that day
    assert s.status == "triggered" and s.day == DAY


def test_real_infy_fixture():
    b = pd.read_csv(FIX / "INFY-5m-2026-10-28.csv", index_col=0)
    b.index = pd.to_datetime(b.index)
    s = check_orb(b, at=at(10, 0))
    assert s.status == "waiting" and s.range_high == 1130.0 and s.range_low == 1107.0
    later = check_orb(b, at=at(11, 0))
    assert later.status == "expired" and "10:20 bar" in later.reason
    assert later.to_dict()["day"] == "2026-10-28"
