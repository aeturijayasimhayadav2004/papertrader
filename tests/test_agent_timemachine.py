from __future__ import annotations

import datetime as dt
import time

import numpy as np
import pandas as pd
import pytest

from papertrader.agent.exitplan import plan_trade
from papertrader.agent.market import IST
from papertrader.agent.timemachine import (describe_odds, describe_session, odds_check, replay_orb_history,
                                           session_matrix, signal_odds, simulate_session)
from papertrader.agent.types import BLOCK, INFO, UNKNOWN
from papertrader.costs import flat, india_delivery
from papertrader.exits import ExitRules
from papertrader.strategies.base import Strategy

from tests.conftest import make_ohlc, random_walk


def sessions(n_days=40, drift=0.0, vol=0.002, seed=0, start="2026-08-03", price=1000.0) -> pd.DataFrame:
    """Synthetic 5-minute NSE sessions (75 bars, 09:15-15:25 starts, IST)."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, periods=n_days)
    idx = pd.DatetimeIndex([pd.Timestamp(dt.datetime.combine(d.date(), dt.time(9, 15)), tz=IST)
                            + pd.Timedelta(minutes=5 * j) for d in days for j in range(75)])
    rets = rng.normal(drift, vol, len(idx))
    close = price * np.exp(np.cumsum(rets))
    open_ = np.r_[price, close[:-1]]
    hi = np.maximum(open_, close) * (1 + rng.uniform(0, vol / 2, len(idx)))
    lo = np.minimum(open_, close) * (1 - rng.uniform(0, vol / 2, len(idx)))
    return pd.DataFrame({"Open": open_, "High": hi, "Low": lo, "Close": close, "Volume": 1000.0}, index=idx)


def split_today(bars: pd.DataFrame, upto: dt.time):
    last_day = bars.index[-1].date()
    hist = bars[bars.index.date < last_day]
    today = bars[(bars.index.date == last_day) & (bars.index.time < upto)]
    return hist, today


def plan_at(price, risk=5.0, r=1.0):
    return plan_trade("INFY.NS", price, stop_method="price", stop_price=price - risk, target_r=r)


def test_session_matrix_grid():
    bars = sessions(3)
    dates, M = session_matrix(bars)
    assert len(dates) == 3 and M["Close"].shape == (3, 75)
    assert np.isfinite(M["Close"]).all()


def test_simulate_deterministic_and_fast():
    hist, today = split_today(sessions(45, seed=3), dt.time(10, 15))
    plan = plan_at(float(today["Close"].iloc[-1]))
    t0 = time.perf_counter()
    a = simulate_session(hist, today, plan, n=10_000, seed=19)
    elapsed = time.perf_counter() - t0
    b = simulate_session(hist, today, plan, n=10_000, seed=19)
    c = simulate_session(hist, today, plan, n=10_000, seed=7)
    assert elapsed < 2.0
    assert a == b and a != c
    assert a["ok"] and a["sessions_used"] == 44 and a["bars_left"] == 72 - 12
    assert a["p_target"] + a["p_stop"] + a["p_open"] == pytest.approx(1.0, abs=1e-3)
    assert "simulation, not a prediction" in describe_session(a)


def test_drift_moves_the_odds():
    up_h, up_t = split_today(sessions(45, drift=0.0008, seed=1), dt.time(10, 0))
    dn_h, dn_t = split_today(sessions(45, drift=-0.0008, seed=1), dt.time(10, 0))
    pu, pd_ = float(up_t["Close"].iloc[-1]), float(dn_t["Close"].iloc[-1])
    up = simulate_session(up_h, up_t, plan_at(pu, risk=pu * 0.01), n=4000)
    dn = simulate_session(dn_h, dn_t, plan_at(pd_, risk=pd_ * 0.01), n=4000)
    assert up["p_target"] > up["p_stop"]
    assert dn["p_stop"] > dn["p_target"]
    assert up["mean_r"] > 0 > dn["mean_r"]


def test_stop_wins_ties_within_a_bar():
    bars = sessions(30, vol=0.0001, seed=2)
    bars["High"] = bars["Close"] * 1.05     # every bar touches both sides
    bars["Low"] = bars["Close"] * 0.95
    hist, today = split_today(bars, dt.time(11, 0))
    res = simulate_session(hist, today, plan_at(float(today["Close"].iloc[-1]), risk=5), n=2000)
    assert res["p_stop"] == 1.0 and res["p_target"] == 0.0


def test_today_is_excluded_from_history_and_too_little_history():
    bars = sessions(10, seed=4)
    _, today = split_today(bars, dt.time(10, 0))
    res = simulate_session(bars, today, plan_at(float(today["Close"].iloc[-1])), n=100, min_sessions=5)
    assert res["sessions_used"] == 9
    small = simulate_session(bars, today, plan_at(float(today["Close"].iloc[-1])), n=100)
    assert small["ok"] is False and "need 20" in small["reason"]


def test_nothing_left_after_flat_by_or_already_stopped():
    hist, today = split_today(sessions(25, seed=5), dt.time(15, 20))
    px = float(today["Close"].iloc[-1])
    res = simulate_session(hist, today, plan_at(px), n=100)
    assert res["bars_left"] == 0 and res["p_open"] == 1.0
    res = simulate_session(hist, today, plan_at(px + 10, risk=5), n=100)   # price already under the stop
    assert res["p_stop"] == 1.0


def test_empty_today_starts_at_entry_from_the_open():
    hist = sessions(25, seed=6)
    res = simulate_session(hist, pd.DataFrame(), plan_at(1000.0), n=500)
    assert res["ok"] and res["bars_left"] == 72 and res["start_price"] == 1000.0


def _day(d: dt.date, rows):
    idx = [pd.Timestamp(dt.datetime.combine(d, dt.time(9, 15)), tz=IST) + pd.Timedelta(minutes=5 * j)
           for j in range(len(rows))]
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=pd.DatetimeIndex(idx)).assign(Volume=1.0)


def test_replay_orb_history_outcomes():
    rng3 = [(100, 101, 99, 100.5)] * 3                    # opening range 99-101, mid 100
    flat_rest = [(101.5, 101.6, 101.4, 101.5)] * 72
    win = rng3 + [(100.5, 101.6, 100.4, 101.5), (101.5, 103.2, 101.4, 103.0)] + flat_rest[:70]
    loss = rng3 + [(100.5, 101.6, 100.4, 101.5), (101.5, 101.5, 99.5, 99.8)] + flat_rest[:70]
    none = rng3 + [(100.5, 100.9, 100.0, 100.5)] * 72
    late = rng3 + [(100.5, 100.9, 100.0, 100.5)] * 66 + [(100.5, 102, 100.4, 101.8)] * 6   # breakout after 14:30
    bars = pd.concat([_day(dt.date(2026, 10, 26), win), _day(dt.date(2026, 10, 27), loss),
                      _day(dt.date(2026, 10, 28), none), _day(dt.date(2026, 10, 29), late)])
    res = replay_orb_history(bars, or_minutes=15, stop_method="or_mid", target_r=1.0)
    assert (res["sessions"], res["fired"], res["target"], res["stop"], res["neither"]) == (4, 2, 1, 1, 0)
    d0 = res["days"][0]
    assert d0["entry"] == 101.5 and d0["stop"] == 100.0 and d0["target"] == 103.0 and d0["entry_time"] == "09:35"
    assert res["days"][1]["r"] == pytest.approx(-1.0)
    assert res["win_rate"] == 0.5 and res["avg_r"] == pytest.approx(0.0)


class EveryTen(Strategy):
    """Buy every 10th bar, sell 5 bars later (a deliberately dumb rule)."""
    name = "every_ten"

    @property
    def warmup(self) -> int:
        return 1

    def frame(self, df):
        i = np.arange(len(df))
        sig = np.where(i % 10 == 0, "BUY", np.where(i % 10 == 5, "SELL", "HOLD"))
        return pd.DataFrame({"signal": sig}, index=df.index)

    def explain(self, row):
        return "every tenth bar"


def trend(n=400, step=-0.003):
    return make_ohlc(100 * np.exp(np.cumsum(np.full(n, step))))


def test_signal_odds_and_block_on_negative_expectancy():
    exits = ExitRules(stop_loss_pct=5)
    res = signal_odds(trend(), EveryTen(), exits, india_delivery(), ticker="DOWN.NS")
    assert res["n_trades"] >= 30 and res["expectancy_pct"] < 0 and res["win_rate"] == 0.0
    f = odds_check(trend(), EveryTen(), exits, india_delivery(), ticker="DOWN.NS")
    assert f.desk == "odds" and f.status == BLOCK and "after costs" in f.summary
    up = odds_check(trend(step=0.003), EveryTen(), exits, flat(0.0005), ticker="UP.NS")
    assert up.status == INFO and up.data["expectancy_pct"] > 0 and up.data["win_rate"] == 1.0
    assert "not a prediction" in describe_odds(up.data)


def test_odds_block_on_low_win_rate_and_too_few():
    exits = ExitRules(stop_loss_pct=5)
    f = odds_check(trend(step=0.003), EveryTen(), exits, flat(0.0), min_win_rate=1.01)
    assert f.status == BLOCK and "won only" in f.summary
    few = odds_check(trend(n=40), EveryTen(), exits, flat(0.0), min_signals=8)
    assert few.status == INFO and "Too few past signals" in few.summary


def test_odds_lookback_and_never_raises():
    df = random_walk(800, seed=1)
    res = signal_odds(df, EveryTen(), ExitRules(), flat(0.001), lookback_days=200)
    assert res["from"] >= (df.index[-1] - pd.Timedelta(days=200)).date().isoformat()
    assert odds_check(pd.DataFrame(), EveryTen(), ExitRules(), flat(0.0)).status == UNKNOWN

    class Broken(EveryTen):
        def frame(self, df):
            raise RuntimeError("boom")
    assert odds_check(df, Broken(), ExitRules(), flat(0.0)).status == UNKNOWN
