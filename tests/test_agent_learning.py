"""Offline tests for the learning desk: forward returns, scorecards, suggestions, history backfills."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from papertrader.agent import learning as L
from papertrader.agent import scanner, timemachine
from papertrader.config import AgentConfig
from papertrader.costs import flat
from papertrader.exits import ExitRules
from papertrader.strategies import MACrossover
from papertrader.strategies.base import Strategy
from tests.conftest import make_ohlc, random_walk


def _df(closes, opens=None, start="2026-01-05"):
    return make_ohlc(closes, start=start, opens=opens)


# ---------------------------------------------------------------- forward returns

def test_forward_return_enters_at_next_open_and_marks_pending():
    closes = [100, 101, 102, 103, 104, 105]
    opens = [100, 110, 101, 102, 103, 104]          # day 1 opens at 110: that is the fill
    df = _df(closes, opens)
    day0 = df.index[0].date()
    fr = L.forward_returns({"A.NS": df}, "A.NS", day0, horizons=(1, 3, 5, 10))
    assert fr["entry_date"] == df.index[1].date() and fr["entry_price"] == 110
    assert fr["horizons"][1]["ret_pct"] == pytest.approx(100 * (101 / 110 - 1), abs=1e-3)   # same-day close
    assert fr["horizons"][3]["ret_pct"] == pytest.approx(100 * (103 / 110 - 1), abs=1e-3)
    assert fr["horizons"][3]["exit_date"] == df.index[3].date()
    assert fr["horizons"][5]["status"] == "done"
    assert fr["horizons"][10] == {"status": "pending", "ret_pct": None, "excess_pct": None, "mae_pct": None,
                                  "exit_date": None}
    low = df["Low"].iloc[1:4].min()
    assert fr["horizons"][3]["mae_pct"] == pytest.approx(100 * (low / 110 - 1), abs=1e-3)

    # signal on the last bar: nothing to fill yet; unknown stock: no_data
    last = L.forward_returns({"A.NS": df}, "A.NS", df.index[-1].date(), horizons=(1,))
    assert last["horizons"][1]["status"] == "pending" and last["entry_date"] is None
    assert L.forward_returns({}, "B.NS", day0, (1,))["horizons"][1]["status"] == "no_data"


def test_forward_return_excess_vs_benchmark_and_holiday_as_of():
    df = _df([100, 100, 110, 120], opens=[100, 100, 100, 110])
    bench = _df([1000, 1000, 1050, 1100], opens=[1000, 1000, 1000, 1050])
    sat = (df.index[0] + pd.Timedelta(days=5)).date()   # 2026-01-10 is a Saturday: next bar is Monday
    fr = L.forward_returns({"A.NS": df}, "A.NS", df.index[0].date(), (2,), benchmark=bench)
    assert fr["horizons"][2]["ret_pct"] == pytest.approx(10.0)
    assert fr["horizons"][2]["excess_pct"] == pytest.approx(10.0 - 5.0)
    assert L.forward_returns({"A.NS": df}, "A.NS", sat, (1,))["horizons"][1]["status"] == "pending"


def test_brief_outcomes_flatten_desks_and_parse_json():
    df = random_walk(60, seed=1, start="2026-01-01")
    day = df.index[10].date()
    findings = [{"desk": "events", "status": "ok", "summary": "", "data": {}},
                {"desk": "odds", "status": "block", "summary": "", "data": {}},
                {"desk": "brain", "status": "info", "summary": "", "data": {"decision": "VETO"}}]
    import json
    briefs = [{"id": f"{day}|A.NS|ma_crossover", "as_of_date": day.isoformat(), "ticker": "A.NS",
               "strategy": "ma_crossover", "verdict": "BLOCK", "findings": json.dumps(findings)}]
    rows = L.brief_outcomes(briefs, {"A.NS": df}, horizons=(1, 5, 100))
    assert [r["id"] for r in rows] == [f"{day}|A.NS|ma_crossover|{h}" for h in (1, 5, 100)]
    r = rows[1]
    assert r["events_status"] == "ok" and r["odds_status"] == "block" and r["brain_decision"] == "veto"
    assert r["status"] == "done" and rows[2]["status"] == "pending"
    db = L.outcome_rows(rows)
    assert set(db[0]) == set(L.OUTCOME_KEYS) and db[0]["desks"]["odds_status"] == "block"

    scans = [{"id": f"{day}|A.NS|breakout", "as_of_date": day, "ticker": "A.NS", "setup": "breakout", "score": 4}]
    srows = L.scan_outcomes(scans, {"A.NS": df}, horizons=(5,))
    assert srows[0]["kind"] == "scan" and srows[0]["setup"] == "breakout" and srows[0]["status"] == "done"
    assert set(L.outcome_rows(srows)[0]) == set(L.OUTCOME_KEYS)


# ---------------------------------------------------------------- scorecards

def _fake(n_block, n_go, block_ret, go_ret, h=5):
    rows = []
    for i in range(n_block):
        rows.append({"id": f"b{i}|{h}", "brief_id": f"b{i}", "kind": "brief", "verdict": "BLOCK",
                     "horizon_days": h, "ret_pct": block_ret, "excess_pct": block_ret - 1, "status": "done",
                     "odds_status": "block", "news_status": "ok"})
    for i in range(n_go):
        rows.append({"id": f"g{i}|{h}", "brief_id": f"g{i}", "kind": "brief", "verdict": "GO",
                     "horizon_days": h, "ret_pct": go_ret, "excess_pct": go_ret - 1, "status": "done",
                     "odds_status": "info", "news_status": "ok"})
    rows.append({"id": "p|5", "kind": "brief", "verdict": "GO", "horizon_days": h, "ret_pct": None,
                 "status": "pending", "odds_status": "info"})
    return rows


def test_desk_scorecard_aggregates_and_flags_small_samples():
    rows = L.desk_scorecard(_fake(4, 6, -2.0, 1.0))
    by = {(r["kind"], r["key"], r["status"]): r for r in rows}
    allr = by[("verdict", "all", "all")]
    assert allr["n"] == 10 and allr["mean_ret_pct"] == pytest.approx(-0.2)    # pending row ignored
    assert allr["hit_rate"] == pytest.approx(0.6) and allr["median_ret_pct"] == pytest.approx(1.0)
    blk = by[("desk", "odds", "block")]
    assert blk["n"] == 4 and blk["mean_excess_pct"] == pytest.approx(-3.0)
    assert blk["avoided_loss_rate"] == pytest.approx(1.0) and blk["baseline_loss_rate"] == pytest.approx(0.4)
    assert L.TOO_FEW in blk["note"] and "avoided loss rate 100%" in blk["note"]
    assert by[("verdict", "verdict", "BLOCK")]["avoided_loss_rate"] == pytest.approx(1.0)
    assert by[("desk", "odds", "info")]["avoided_loss_rate"] is None
    assert by[("desk", "news", "ok")]["n"] == 10

    big = {(r["kind"], r["key"], r["status"]): r for r in L.desk_scorecard(_fake(25, 30, -2.0, 1.0))}
    assert big[("desk", "odds", "block")]["enough"] and L.TOO_FEW not in big[("desk", "odds", "block")]["note"]

    sc_rows = L.scorecard_rows(desk_rows=rows, as_of=dt.date(2026, 10, 29))
    assert all(tuple(r) == L.SCORECARD_KEYS for r in sc_rows)
    assert "desk|odds|block|5" in {r["id"] for r in sc_rows}
    assert len({r["id"] for r in sc_rows}) == len(sc_rows)


def test_setup_scorecard_groups_by_setup():
    outs = [{"id": f"{i}|5", "kind": "scan", "setup": "breakout" if i % 2 else "oversold", "horizon_days": 5,
             "ret_pct": float(i), "excess_pct": None, "status": "done"} for i in range(6)]
    rows = {r["key"]: r for r in L.setup_scorecard(outs)}
    assert rows["all"]["n"] == 6 and rows["breakout"]["n"] == 3 and rows["breakout"]["mean_ret_pct"] == 3.0
    assert rows["oversold"]["baseline_n"] == 6 and rows["breakout"]["mean_excess_pct"] is None


def test_suggestions_wording_and_never_on_small_samples():
    s = L.suggestions(L.desk_scorecard(_fake(1, 2, -1.0, 1.0)), [], None)
    assert any("Too few finished briefs yet (n=3" in x for x in s)
    assert not any("keep it" in x for x in s)

    s = L.suggestions(L.desk_scorecard(_fake(25, 40, -2.0, 1.0)), [], None)
    assert any("odds desk 'block'" in x and "keep it" in x and "100%" in x for x in s)

    def ev(nb, na, rb, ra):
        st = lambda n, r: {"n": n, "win_rate": 0.5, "mean_ret_pct": r, "median_ret_pct": r, "sum_ret_pct": None,
                           "enough": n >= 20}
        return {"start": "2021-01-01", "end": "2026-01-01", "tickers": 10, "strategies": ["x"],
                "groups": {"blocked": st(nb, rb), "allowed": st(na, ra), "too_few": st(0, None)},
                "params": {"odds_min_signals": 8, "odds_min_win_rate": 0.4}, "by_strategy": {}}

    keep = L.suggestions([], [], ev(34, 120, -1.2, 0.8))
    assert any("34 trades it would have BLOCKED averaged -1.20%" in x and "(n=34/120)" in x and "keep it" in x
               for x in keep)
    loosen = L.suggestions([], [], ev(34, 120, 1.5, 0.2))
    assert any("consider loosening" in x and "Suggestion only" in x for x in loosen)
    few = L.suggestions([], [], ev(5, 120, -3.0, 0.8))
    assert any("too few to judge" in x for x in few) and not any("keep it" in x for x in few)

    hist = [{"kind": "setup_history", "key": "breakout", "status": "all", "horizon_days": 20, "n": 400,
             "mean_ret_pct": 2.0, "mean_excess_pct": 1.1, "years_positive": "4/5", "tickers": 30},
            {"kind": "setup_history", "key": "gap_down", "status": "all", "horizon_days": 20, "n": 7,
             "mean_ret_pct": -2.0, "mean_excess_pct": -3.0, "years_positive": "0/2", "tickers": 5}]
    hs = L.suggestions([], [], None, hist)
    assert any(x.startswith("breakout") and "historical edge" in x and "n=400" in x for x in hs)
    assert any(x.startswith("gap_down") and "too few to judge" in x for x in hs)


# ---------------------------------------------------------------- history: setup flags == scanner

def _spiky(n=420, seed=3):
    """A random walk with volume spikes, runs and gaps so every setup fires somewhere."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0005, 0.02, n)
    rets[rng.choice(n, 15, replace=False)] += rng.choice([-0.08, 0.08], 15)
    closes = 100 * np.exp(np.cumsum(rets))
    opens = np.r_[closes[0], closes[:-1]] * np.exp(rng.normal(0, 0.012, n))
    df = make_ohlc(closes, start="2023-01-02", opens=opens)
    df["Volume"] = 1e6 * np.exp(rng.normal(0, 0.5, n)) * np.where(rets > 0.05, 3.0, 1.0)
    return df


def test_vectorised_setup_flags_equal_scan_daily_on_sampled_days():
    prices = {f"S{k}.NS": _spiky(seed=k) for k in range(4)}
    seen: set[str] = set()
    for t, df in prices.items():
        flags = L.setup_flags(df)
        for i in list(range(25, 40)) + list(range(40, len(df), 3)):
            day = df.index[i]
            hits = scanner.scan_daily({t: df.loc[:day]})
            want = {h["setup"] for h in hits}
            got = {s for s in scanner.SETUPS if flags.loc[day, s]} if i >= scanner.MIN_BARS - 1 else set()
            assert got == want, (t, day.date(), got, want)
            seen |= want
    assert {"breakout", "oversold", "gap_up", "gap_down", "near_52w_high", "reclaim_ma20"} <= seen


def test_historical_setup_scorecard_excess_vs_universe():
    prices = {f"S{k}.NS": _spiky(seed=k) for k in range(5)}
    bench = _spiky(seed=99)
    rows = L.historical_setup_scorecard(prices, bench, dt.date(2023, 6, 1), dt.date(2024, 6, 30), horizons=(5,))
    by = {r["key"]: r for r in rows}
    assert by["all"]["mean_excess_pct"] == pytest.approx(0.0, abs=1e-9)   # the universe vs itself
    assert "benchmark" in by and all(r["horizon_days"] == 5 for r in rows)
    some = next(r for r in rows if r["key"] not in ("all", "benchmark"))
    assert some["n"] > 0 and "/" in some["years_positive"]
    stepped = {r["key"]: r for r in L.historical_setup_scorecard(prices, None, dt.date(2023, 6, 1),
                                                                 dt.date(2024, 6, 30), (5,), step_days=5)}
    assert stepped["all"]["n"] < by["all"]["n"] / 4 and "benchmark" not in stepped


# ---------------------------------------------------------------- history: the odds desk

class EveryTen(Strategy):
    """Buy every 10th bar, sell 5 bars later (causal: depends only on the bar number)."""
    name = "every_ten"
    warmup = 1

    def frame(self, df):
        sig = np.where(np.arange(len(df)) % 10 == 0, "BUY", np.where(np.arange(len(df)) % 10 == 5, "SELL", "HOLD"))
        return pd.DataFrame({"signal": sig}, index=df.index)

    def explain(self, row):
        return ""


def _agent(**kw):
    return AgentConfig(enabled=True, odds_min_signals=kw.get("min_signals", 8),
                       odds_min_win_rate=kw.get("min_win_rate", 0.4), odds_lookback_days=kw.get("lookback", 400))


def test_historical_odds_evaluation_groups_and_uses_only_past_data(monkeypatch):
    df = random_walk(900, seed=5, start="2019-01-01")
    seen = []
    real = timemachine.odds_check

    def spy(past, strategy, *a, **k):
        seen.append(past.index[-1])
        return real(past, strategy, *a, **k)

    monkeypatch.setattr(timemachine, "odds_check", spy)
    ev = L.historical_odds_evaluation({"A.NS": df}, [EveryTen(), MACrossover(5, 20)], ExitRules(), flat(0.001),
                                      _agent(), dt.date(2020, 1, 1), dt.date(2022, 6, 30))
    trades = ev["trades"]
    assert trades and len(seen) == len(trades)
    for t, last_bar in zip(trades, seen):
        assert last_bar.date() == t["signal_date"] < t["entry_date"]        # nothing after the signal day
        assert t["odds_data_to"] == t["signal_date"].isoformat()
    g = ev["groups"]
    assert g["all"]["n"] == len(trades) == sum(g[k]["n"] for k in ("blocked", "allowed", "too_few", "unknown"))
    assert g["not_blocked"]["n"] == g["all"]["n"] - g["blocked"]["n"]
    assert g["too_few"]["n"] > 0 and g["allowed"]["n"] + g["blocked"]["n"] > 0
    assert set(ev["by_strategy"]) == {"every_ten", "ma_crossover"}

    rows = L.scorecard_rows(odds_eval=ev)
    assert all(tuple(r) == L.SCORECARD_KEYS and r["kind"] == "odds_history" for r in rows)


def test_historical_odds_verdicts_ignore_the_future():
    """Rewrite every bar after a cut-off: verdicts for trades signalled before it must not change."""
    df = random_walk(900, seed=7, start="2019-01-01")
    cut = df.index[700]
    alt = df.copy()
    alt.loc[alt.index > cut, ["Open", "High", "Low", "Close"]] *= np.linspace(0.5, 2.0, int((alt.index > cut).sum()))[:, None]
    args = ([EveryTen()], ExitRules(), flat(0.001), _agent(min_signals=3), dt.date(2020, 1, 1), dt.date(2022, 6, 30))
    a = {t["signal_date"]: t for t in L.historical_odds_evaluation({"A.NS": df}, *args)["trades"]}
    b = {t["signal_date"]: t for t in L.historical_odds_evaluation({"A.NS": alt}, *args)["trades"]}
    early = [d for d in a if pd.Timestamp(d) < cut - pd.Timedelta(days=30)]
    assert early
    for d in early:
        for k in ("odds_status", "group", "past_n", "past_win_rate", "past_avg_pct"):
            assert a[d][k] == b[d][k], (d, k)


def test_learning_options_defaults():
    o = L.LearningOptions.from_dict(None)
    assert o.horizons == (1, 5, 20) and o.min_n == 20
    assert L.LearningOptions.from_dict({"horizons": [2, 10]}).horizons == (2, 10)
