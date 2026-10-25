import datetime as dt

import numpy as np
import pandas as pd
import pytest

from papertrader.backtest import assert_no_lookahead, buy_and_hold, run_backtest, simulate_sleeve
from papertrader.metrics import max_drawdown
from papertrader.selection import evaluate_strategies
from papertrader.strategies import Breakout, MACrossover, RSIBounce, Signal, Strategy
from tests.conftest import make_ohlc, random_walk

COST = 0.002
ALL = [MACrossover(20, 50), RSIBounce(14, 30, 70), Breakout(20, 10)]


def signals_at(df, buys=(), sells=()):
    s = pd.Series(Signal.HOLD.value, index=df.index)
    for i in buys:
        s.iloc[i] = Signal.BUY.value
    for i in sells:
        s.iloc[i] = Signal.SELL.value
    return s


def period(df):
    return df.index[0].date(), df.index[-1].date()


# ---------- fill timing and costs ----------

def test_signal_fills_at_next_days_open_not_same_day():
    closes = [100, 101, 102, 103, 104, 105]
    opens = [100, 150, 160, 170, 180, 190]  # distinctive opens
    df = make_ohlc(closes, opens=opens)
    res = simulate_sleeve(df, signals_at(df, buys=[1], sells=[3]), *period(df), 10_000, COST)
    (trade,) = res.trades
    assert trade.signal_date == df.index[1]
    assert trade.entry_date == df.index[2] and trade.entry_price == 160  # open of day 2
    assert trade.exit_date == df.index[4] and trade.exit_price == 180  # open after SELL on day 3


def test_round_trip_at_flat_prices_loses_exactly_the_costs():
    df = make_ohlc([100.0] * 6)
    res = simulate_sleeve(df, signals_at(df, buys=[0], sells=[2]), *period(df), 10_000, COST)
    (trade,) = res.trades
    assert trade.return_pct == pytest.approx((1 - COST) / (1 + COST) - 1)
    assert res.equity.iloc[-1] == pytest.approx(10_000 * (1 - COST) / (1 + COST))


def test_open_position_is_charged_exit_cost_at_end():
    df = make_ohlc([100.0] * 4)
    res = simulate_sleeve(df, signals_at(df, buys=[0]), *period(df), 10_000, COST)
    assert res.trades[-1].exit_reason == "end_of_period"
    assert res.equity.iloc[-1] == pytest.approx(10_000 * (1 - COST) / (1 + COST))


def test_stop_loss_exits_at_stop_price():
    closes = [100, 100, 100, 97, 97]
    lows = [99, 99, 99, 94, 96]  # day 3 trades through the 95 stop
    df = make_ohlc(closes, opens=[100] * 5, lows=lows)
    res = simulate_sleeve(df, signals_at(df, buys=[0]), *period(df), 10_000, COST, stop_loss_pct=5)
    (trade,) = res.trades
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(95.0)
    assert trade.exit_date == df.index[3]


def test_stop_loss_gap_down_exits_at_open():
    df = make_ohlc([100, 100, 90, 90], opens=[100, 100, 90, 90], lows=[99, 99, 88, 89])
    res = simulate_sleeve(df, signals_at(df, buys=[0]), *period(df), 10_000, COST, stop_loss_pct=5)
    assert res.trades[0].exit_price == 90  # opened below the 95 stop


def test_buy_and_hold_matches_hand_calculation():
    df = make_ohlc([100, 110, 120], opens=[100, 105, 115])
    res = buy_and_hold({"X": df}, *period(df), 10_000, COST)
    qty = 10_000 / (100 * (1 + COST))
    assert res.equity.iloc[-1] == pytest.approx(qty * 120 * (1 - COST))


def test_portfolio_splits_capital_equally():
    a, b = make_ohlc([100.0] * 5), make_ohlc([50.0] * 5)
    res = buy_and_hold({"A": a, "B": b}, *period(a), 100_000, 0.0)
    assert res.equity.iloc[-1] == pytest.approx(100_000)
    assert len(res.trades) == 2


def test_max_drawdown():
    eq = pd.Series([100, 120, 90, 130], index=pd.bdate_range("2020-01-01", periods=4))
    assert max_drawdown(eq, 100) == pytest.approx(90 / 120 - 1)


# ---------- lookahead bias ----------

@pytest.mark.parametrize("strategy", ALL, ids=lambda s: s.name)
def test_signals_do_not_depend_on_future_bars(strategy):
    assert_no_lookahead(strategy, random_walk(500, seed=11), checkpoints=60)


class PeekingStrategy(Strategy):
    """Deliberately cheats: buys if TOMORROW's close is higher."""
    name, label = "peek", "cheater"
    warmup = 2

    def frame(self, df):
        nxt = df.Close.shift(-1)
        sig = np.where(nxt > df.Close, Signal.BUY.value, Signal.SELL.value)
        return pd.DataFrame({"signal": sig}, index=df.index)

    def explain(self, row):
        return "peeks"


def test_lookahead_checker_catches_a_cheating_strategy():
    with pytest.raises(AssertionError, match="lookahead"):
        assert_no_lookahead(PeekingStrategy(), random_walk(300, seed=2))


@pytest.mark.parametrize("strategy", ALL, ids=lambda s: s.name)
def test_changing_future_prices_does_not_change_past_results(strategy):
    df = random_walk(500, seed=5)
    cut = 350
    shocked = df.copy()
    shocked.iloc[cut + 1:] *= 3.0  # wildly different future
    start, end = df.index[60].date(), df.index[-1].date()
    a = run_backtest(strategy, {"X": df}, start, end, 100_000, COST, stop_loss_pct=5)
    b = run_backtest(strategy, {"X": shocked}, start, end, 100_000, COST, stop_loss_pct=5)
    upto = df.index[cut]
    pd.testing.assert_series_equal(a.equity.loc[:upto], b.equity.loc[:upto])
    done_a = [t for t in a.trades if t.exit_date <= upto and t.exit_reason != "end_of_period"]
    done_b = [t for t in b.trades if t.exit_date <= upto and t.exit_reason != "end_of_period"]
    assert done_a == done_b


@pytest.mark.parametrize("strategy", ALL, ids=lambda s: s.name)
def test_every_fill_happens_after_its_signal(strategy):
    df = random_walk(600, seed=9)
    res = run_backtest(strategy, {"X": df}, df.index[60].date(), df.index[-1].date(), 100_000, COST, 5)
    assert res.trades, "expected some trades on a random walk"
    for t in res.trades:
        assert t.entry_date > t.signal_date
        assert t.entry_price == df.Open.loc[t.entry_date]


def test_backtest_ignores_bars_after_period_end():
    df = random_walk(400, seed=4)
    end = df.index[300].date()
    a = run_backtest(MACrossover(5, 20), {"X": df}, df.index[30].date(), end, 100_000, COST)
    b = run_backtest(MACrossover(5, 20), {"X": df.loc[:pd.Timestamp(end)]}, df.index[30].date(), end, 100_000, COST)
    pd.testing.assert_series_equal(a.equity, b.equity)


# ---------- selection ----------

class NeverTrades(Strategy):
    name, label, warmup = "never", "never trades", 1

    def frame(self, df):
        return pd.DataFrame({"signal": Signal.HOLD.value}, index=df.index)

    def explain(self, row):
        return "nothing"


def test_selection_holds_cash_when_nothing_beats_buy_and_hold(cfg):
    # Steadily rising market: sitting in cash cannot beat buy-and-hold.
    up = make_ohlc(np.linspace(100, 300, 700), start="2018-01-01")
    sel = evaluate_strategies([NeverTrades()], {"AAA.NS": up, "BBB.NS": up}, cfg, up.index[-1].date())
    assert sel.selected == []
    assert "holds cash" in sel.summary()
    assert not sel.evaluations[0].qualified


def test_selection_uses_only_data_up_to_as_of(cfg):
    df = random_walk(700, seed=1, start="2018-01-01")
    as_of = df.index[500].date()
    sel = evaluate_strategies([MACrossover(5, 20)], {"AAA.NS": df}, cfg, as_of)
    assert sel.periods["out_of_sample"][1] == as_of
    assert sel.evaluations[0].out_of_sample.equity.index[-1].date() <= as_of
    rows = sel.to_rows()
    assert {r["period"] for r in rows} == {"in_sample", "out_of_sample"}
    assert any(r["strategy"] == "buy_and_hold" for r in rows)


def test_selection_reports_missing_test_period(cfg):
    df = random_walk(200, seed=1, start="2018-01-01")
    sel = evaluate_strategies([MACrossover(5, 20)], {"AAA.NS": df}, cfg, dt.date(2018, 9, 1))
    assert sel.selected == [] and sel.problems
