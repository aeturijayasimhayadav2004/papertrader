import numpy as np
import pandas as pd
import pytest

from papertrader.indicators import rsi
from papertrader.strategies import Breakout, MACrossover, RSIBounce, Signal, build_strategies
from tests.conftest import make_ohlc, random_walk

ALL = [MACrossover(20, 50), RSIBounce(14, 30, 70), Breakout(20, 10)]


def test_ma_crossover_buys_on_the_cross_bar_only():
    # 60 falling days then a sharp rise: fast SMA crosses above slow SMA once.
    closes = list(np.linspace(200, 100, 60)) + list(np.linspace(101, 220, 40))
    df = make_ohlc(closes)
    sig = MACrossover(5, 20).signals(df)
    buys = sig[sig == Signal.BUY.value]  # .value: pandas' python-backed str dtype won't match the enum
    assert len(buys) == 1
    fast = df.Close.rolling(5).mean()
    slow = df.Close.rolling(20).mean()
    t = df.index.get_loc(buys.index[0])
    assert fast.iloc[t] > slow.iloc[t] and fast.iloc[t - 1] <= slow.iloc[t - 1]


def test_rsi_is_bounded_and_matches_extremes():
    up = rsi(pd.Series(np.arange(1, 60, dtype=float)), 14)
    assert up.dropna().eq(100).all()
    r = rsi(random_walk().Close, 14).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_rsi_bounce_buys_when_rsi_recovers_above_30():
    closes = list(np.linspace(100, 60, 30)) + [62, 65, 68, 72]
    df = make_ohlc(closes)
    fr = RSIBounce(14, 30, 70).frame(df)
    buys = fr[fr.signal == Signal.BUY.value]
    assert len(buys) >= 1
    row = buys.iloc[0]
    assert row.rsi >= 30 > row.rsi_prev


def test_breakout_uses_prior_high_not_todays_high():
    closes = [100.0] * 25 + [110.0]
    df = make_ohlc(closes)
    fr = Breakout(20, 10).frame(df)
    assert fr.signal.iloc[-1] == Signal.BUY
    assert fr.prior_high.iloc[-1] == pytest.approx(df.High.iloc[-21:-1].max())


@pytest.mark.parametrize("strategy", ALL, ids=lambda s: s.name)
def test_decide_matches_vectorised_signals_and_explains(strategy):
    df = random_walk(400, seed=3)
    sig = strategy.signals(df)
    for t in (strategy.warmup + 5, 200, 399):
        d = strategy.decide(df.iloc[: t + 1])
        assert d.signal.value == sig.iloc[t]
        assert isinstance(d.reason, str) and len(d.reason) > 10


@pytest.mark.parametrize("strategy", ALL, ids=lambda s: s.name)
def test_decide_with_short_history_holds(strategy):
    d = strategy.decide(random_walk(5))
    assert d.signal == Signal.HOLD and "history" in d.reason


def test_build_strategies_respects_enabled_flag():
    s = build_strategies({"ma_crossover": {"enabled": False, "fast": 5, "slow": 10},
                          "breakout": {"entry_lookback": 10, "exit_lookback": 5}})
    assert [x.name for x in s] == ["breakout"]
    with pytest.raises(ValueError):
        build_strategies({"nope": {}})


def test_signal_compares_equal_on_every_pandas_string_backend():
    # CI has no pyarrow, so pandas uses its pure-Python string storage, which compares via str(other).
    s = pd.Series(["BUY", "HOLD"], dtype=pd.StringDtype("python", na_value=np.nan))
    assert (s == Signal.BUY).tolist() == [True, False]
    assert str(Signal.SELL) == "SELL"
