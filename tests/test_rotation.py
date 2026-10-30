from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tests.conftest import make_ohlc, random_walk
from papertrader.backtest import buy_and_hold
from papertrader.costs import BUY, SELL, flat, india_delivery
from papertrader.portfolio import (
    REGISTRY,
    backtest_rotation,
    build_rotation,
    buy_and_hold_universe,
    index_buy_and_hold,
    rebalance_orders,
    walk_forward,
)
from papertrader.portfolio.backtest import _rebalance_flags

ZERO = flat(0.0)


def universe(n: int = 14, bars: int = 700) -> dict[str, pd.DataFrame]:
    return {f"S{i:02d}.NS": random_walk(n=bars, seed=100 + i) for i in range(n)}


@pytest.fixture(scope="module")
def prices():
    return universe()


@pytest.fixture(scope="module")
def bench():
    return random_walk(n=700, seed=7)


# --------------------------------------------------------------------------- causality


@pytest.mark.parametrize("name", list(REGISTRY))
def test_weights_do_not_use_future_bars(name, prices, bench):
    strat = build_rotation(name)
    idx = bench.index
    for t in idx[[300, 420, 555, 650]]:
        full = strat.weights(prices, t, bench)
        cut = {k: df.loc[:t] for k, df in prices.items()}
        # wreck every bar after t: a causal strategy must not notice
        wild = {}
        for k, df in prices.items():
            d = df.copy()
            d.loc[d.index > t, ["Open", "High", "Low", "Close"]] *= 3.0 if k < "S07" else 0.2
            wild[k] = d
        bwild = bench.copy()
        bwild.loc[bwild.index > t, ["Open", "High", "Low", "Close"]] *= 0.1
        assert strat.weights(cut, t, bench.loc[:t]) == full
        assert strat.weights(wild, t, bwild) == full


@pytest.mark.parametrize("name", list(REGISTRY))
def test_weights_sum_at_most_one_and_explained(name, prices, bench):
    strat = build_rotation(name)
    t = bench.index[500]
    w = strat.weights(prices, t, bench)
    assert sum(w.values()) <= 1.0 + 1e-9
    assert all(v > 0 for v in w.values())
    why = strat.explain(prices, t, bench)
    for ticker in w:
        assert isinstance(why[ticker], str) and why[ticker]


def test_no_stock_picked_before_warmup(prices, bench):
    strat = build_rotation("momentum_12_1")
    assert strat.weights(prices, bench.index[100], bench) == {}


def test_momentum_picks_the_strongest():
    up = make_ohlc(np.linspace(100, 300, 400))
    flat_ = make_ohlc(np.full(400, 100.0))
    down = make_ohlc(np.linspace(300, 100, 400))
    strat = build_rotation("momentum_12_1", {"top_n": 1})
    w = strat.weights({"UP": up, "FLAT": flat_, "DOWN": down}, up.index[-1])
    assert w == {"UP": 1.0}


def test_dual_momentum_leaves_cash_for_falling_names():
    up = make_ohlc(np.linspace(100, 300, 400))
    down = make_ohlc(np.linspace(300, 100, 400))
    strat = build_rotation("dual_momentum")
    w = strat.weights({"UP": up, "DOWN": down}, up.index[-1])
    assert w == {"UP": pytest.approx(0.1)}


def test_regime_filter_goes_to_cash():
    n = 450
    stocks = {f"S{i}": make_ohlc(np.linspace(100, 200 + 10 * i, n)) for i in range(3)}
    rising = make_ohlc(np.linspace(10_000, 20_000, n))
    crash = make_ohlc(np.concatenate([np.linspace(10_000, 20_000, 400), np.linspace(20_000, 9_000, 50)]))
    t = rising.index[-1]
    plain = build_rotation("momentum_12_1")
    filt = build_rotation("momentum_12_1_regime")
    assert len(filt.weights(stocks, t, rising)) == 3
    assert filt.weights(stocks, t, crash) == {}
    assert "_cash" in filt.explain(stocks, t, crash)
    assert filt.weights(stocks, t, None) == {}          # cannot confirm the regime -> cash
    assert len(plain.weights(stocks, t, crash)) == 3    # the plain version ignores the market


def test_trend_caps_weight_per_name():
    n = 300
    stocks = {"UP1": make_ohlc(np.linspace(100, 200, n)), "UP2": make_ohlc(np.linspace(50, 90, n)),
              "DOWN": make_ohlc(np.linspace(200, 100, n))}
    w = build_rotation("trend_sma200").weights(stocks, stocks["UP1"].index[-1])
    assert w == {"UP1": pytest.approx(0.10), "UP2": pytest.approx(0.10)}


def test_unknown_strategy():
    with pytest.raises(KeyError):
        build_rotation("nope")


# --------------------------------------------------------------------------- orders


def test_rebalance_orders_sells_first_and_sizes_integer_shares():
    orders = rebalance_orders({"A": 0.5, "B": 0.25}, {"C": 10, "B": 100},
                              {"A": 99.0, "B": 10.0, "C": 50.0}, equity=10_000.0, costs=ZERO)
    # C has no target -> sold out first; A: floor(5000 / 99) = 50; B: want 250, have 100 -> buy 150
    assert orders == [("C", SELL, 10), ("A", BUY, 50), ("B", BUY, 150)]


def test_rebalance_orders_reserves_room_for_buy_charges():
    cm = flat(0.01)
    (ticker, side, qty), = rebalance_orders({"A": 1.0}, {}, {"A": 100.0}, equity=10_000.0, costs=cm)
    assert (ticker, side) == ("A", BUY)
    assert qty == 99                           # 100 shares would need 10,100 with 1% charges
    assert qty * 100.0 + cm.fee(BUY, qty, 100.0) <= 10_000.0


def test_rebalance_orders_band_skips_small_trims_but_not_exits_or_entries():
    pos = {"A": 100, "B": 10}
    px = {"A": 10.0, "B": 10.0, "N": 10.0}
    orders = rebalance_orders({"A": 0.098, "N": 0.001}, pos, px, equity=10_000.0, costs=ZERO,
                              min_trade_value=500.0)
    assert ("A", SELL, 2) not in orders        # 2 shares = Rs 20 trim, below the band
    assert ("B", SELL, 10) in orders           # full exit always trades
    assert ("N", BUY, 1) in orders             # new entry always trades


def test_rebalance_orders_skips_missing_prices():
    assert rebalance_orders({"A": 0.5}, {"B": 5}, {"B": float("nan")}, 1000.0, ZERO) == []


# --------------------------------------------------------------------------- backtest


def test_rebalance_flags_month_end():
    cal = pd.bdate_range("2021-01-01", "2021-04-15")
    flags = _rebalance_flags(cal, "M")
    assert list(cal[flags].strftime("%Y-%m-%d")) == ["2021-01-29", "2021-02-26", "2021-03-31"]
    w = _rebalance_flags(cal, "W")
    assert all(d.weekday() == 4 for d in cal[w])


def test_fills_at_next_open(prices, bench):
    strat = build_rotation("momentum_12_1")
    res = backtest_rotation(strat, prices, "2019-06-01", "2020-06-30", ZERO, 1_000_000, bench)
    first_day = res.equity.index[0]
    cal = res.equity.index
    decision_dates = sorted(res.targets)
    assert decision_dates[0] < first_day                    # first decision is the close before the period
    for f in res.fills:
        if f.reason == "end_of_period":
            assert f.date == cal[-1]
            continue
        assert f.price == prices[f.ticker].loc[f.date, "Open"]
        prev = cal[cal.get_loc(f.date) - 1] if f.date != first_day else decision_dates[0]
        assert prev in res.targets                         # filled the day after a decision
    assert {f.date for f in res.fills if f.date == first_day}  # invested from the first open


def test_costs_are_charged(prices, bench):
    strat = build_rotation("momentum_12_1")
    free = backtest_rotation(strat, prices, "2019-06-01", "2020-06-30", ZERO, 1_000_000, bench)
    cm = india_delivery()
    paid = backtest_rotation(strat, prices, "2019-06-01", "2020-06-30", cm, 1_000_000, bench)
    assert free.fees == 0.0
    assert paid.fees > 0
    assert paid.fees == pytest.approx(sum(cm.fee(f.side, f.qty, f.price) for f in paid.fills))
    assert paid.equity.iloc[-1] < free.equity.iloc[-1]
    assert paid.metrics["n_trades"] == len(paid.fills)


def test_cash_is_conserved_with_zero_costs():
    # one stock, always fully held: equity should follow the share count times price, plus leftover cash
    df = make_ohlc(np.linspace(100, 150, 400))
    strat = build_rotation("trend_sma200", {"max_weight": 1.0})
    res = backtest_rotation(strat, {"A": df}, "2020-12-01", "2021-06-30", ZERO, 100_000)
    buys = [f for f in res.fills if f.side == BUY]
    qty = sum(f.qty for f in buys) - sum(f.qty for f in res.fills if f.side == SELL and f.reason == "rebalance")
    cash = 100_000 - sum(f.qty * f.price for f in buys)
    assert res.equity.iloc[-2] == pytest.approx(cash + qty * df.loc[res.equity.index[-2], "Close"])


def test_no_bars_after_end_are_seen(prices, bench):
    strat = build_rotation("momentum_12_1")
    a = backtest_rotation(strat, prices, "2019-06-01", "2020-01-31", ZERO, 1_000_000, bench)
    cut = {k: df.loc[:"2020-01-31"] for k, df in prices.items()}
    b = backtest_rotation(strat, cut, "2019-06-01", "2020-01-31", ZERO, 1_000_000, bench.loc[:"2020-01-31"])
    pd.testing.assert_series_equal(a.equity, b.equity)


def test_regime_backtest_sits_in_cash_when_market_falls():
    n = 500
    stocks = {f"S{i}": make_ohlc(np.linspace(100, 200 + 10 * i, n)) for i in range(3)}
    crash = make_ohlc(np.concatenate([np.linspace(10_000, 20_000, 300), np.linspace(20_000, 5_000, 200)]))
    start, end = crash.index[400], crash.index[-1]
    res = backtest_rotation(build_rotation("momentum_12_1_regime"), stocks, start, end, ZERO, 1_000_000, crash)
    assert res.fills == []
    assert (res.equity == 1_000_000).all()


def test_buy_and_hold_universe_matches_project_benchmark(prices):
    cm = india_delivery()
    mine = buy_and_hold_universe(prices, "2019-06-01", "2020-06-30", cm)
    theirs = buy_and_hold(prices, pd.Timestamp("2019-06-01").date(), pd.Timestamp("2020-06-30").date(),
                          1_000_000, cm)
    assert mine.metrics["total_return"] == pytest.approx(theirs.metrics["total_return"])
    # equal weight: the result is about the average stock return, minus costs
    rets = [df.loc["2019-06-03":"2020-06-30"] for df in prices.values()]
    avg = np.mean([r["Close"].iloc[-1] / r["Open"].iloc[0] - 1 for r in rets])
    assert mine.metrics["total_return"] == pytest.approx(avg, abs=0.01)
    assert mine.metrics["total_return"] < avg


def test_index_buy_and_hold(bench):
    res = index_buy_and_hold(bench, "2019-01-01", "2019-12-31")
    period = bench.loc["2019-01-01":"2019-12-31"]
    assert res.metrics["total_return"] == pytest.approx(period["Close"].iloc[-1] / period["Open"].iloc[0] - 1)


def test_walk_forward_folds(prices, bench):
    wf = walk_forward(build_rotation("low_volatility"), prices, "2019-03-01", "2020-08-31", india_delivery(),
                      benchmark=bench, weights_cache={})
    assert len(wf.folds) == 1 or len(wf.folds) == 2
    assert 0.0 <= wf.win_rate <= 1.0
    assert wf.wins == sum(f["won"] for f in wf.folds)
    for f in wf.folds:
        assert f["won"] == (f["strategy_return"] > f["bh_return"])


def test_weights_cache_gives_same_result(prices, bench):
    strat = build_rotation("dual_momentum")
    cache: dict = {}
    a = backtest_rotation(strat, prices, "2019-06-01", "2020-06-30", ZERO, benchmark=bench, weights_cache=cache)
    b = backtest_rotation(strat, prices, "2019-06-01", "2020-06-30", ZERO, benchmark=bench, weights_cache=cache)
    c = backtest_rotation(strat, prices, "2019-06-01", "2020-06-30", ZERO, benchmark=bench)
    pd.testing.assert_series_equal(a.equity, b.equity)
    pd.testing.assert_series_equal(a.equity, c.equity)
