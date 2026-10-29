from __future__ import annotations

import pytest

from papertrader.costs import BUY, SELL, flat, india_delivery, india_intraday
from papertrader.exits import ExitRules

from tests.conftest import make_ohlc


def test_take_profit_r_target():
    rules = ExitRules(stop_loss_pct=5, take_profit_r=2.0)
    # stop 95 -> risk 5 -> 2R target 110
    assert rules.target(100.0) == pytest.approx(110.0)


def test_take_profit_nearer_of_pct_and_r():
    assert ExitRules(stop_loss_pct=5, take_profit_pct=8, take_profit_r=2.0).target(100.0) == pytest.approx(108.0)
    assert ExitRules(stop_loss_pct=5, take_profit_pct=15, take_profit_r=2.0).target(100.0) == pytest.approx(110.0)


def test_take_profit_r_uses_atr_stop():
    rules = ExitRules(stop_loss_pct=10, stop_mode="atr", atr_multiple=2.0, take_profit_r=1.5)
    # ATR 2 -> stop 96 -> risk 4 -> target 106
    assert rules.target(100.0, atr_at_signal=2.0) == pytest.approx(106.0)
    assert rules.target(100.0) == pytest.approx(115.0)  # no ATR: fixed 10% stop


def test_take_profit_r_without_stop_is_none():
    assert ExitRules(stop_loss_pct=None, take_profit_r=2.0).target(100.0) is None
    assert ExitRules().target(100.0) is None


def test_backtest_hits_r_target():
    from papertrader.backtest import simulate_sleeve
    import pandas as pd
    closes = [100, 100, 100, 104, 112, 112]
    df = make_ohlc(closes)
    sig = pd.Series("HOLD", index=df.index)
    sig.iloc[1] = "BUY"
    res = simulate_sleeve(df, sig, df.index[0].date(), df.index[-1].date(), 10_000, 0.0,
                          exits=ExitRules(stop_loss_pct=5, take_profit_r=2.0))
    assert res.trades[0].exit_reason == "take_profit"
    assert res.trades[0].exit_price == pytest.approx(110.0)


def test_india_intraday_charges_itemised():
    cm = india_intraday(slippage_pct=0.0)
    notional = 100_000.0
    exch_sebi_gst = (notional * 0.00297 / 100 + notional * 10 / 1e7) * 1.18
    brokerage = 20.0 * 1.18                      # 0.03% of 1 lakh = 30, capped at 20
    buy = notional * 0.003 / 100 + exch_sebi_gst + brokerage
    sell = notional * 0.025 / 100 + exch_sebi_gst + brokerage
    assert cm.fee(BUY, 1000, 100.0) == pytest.approx(buy)
    assert cm.fee(SELL, 1000, 100.0) == pytest.approx(sell)
    assert cm.sell_fixed == 0.0
    # small order: brokerage 0.03% under the cap
    assert cm.fee(BUY, 10, 100.0) == pytest.approx(1000 * (0.003 / 100) + (1000 * 0.00297 / 100 + 1000 * 10 / 1e7 + 0.3) * 1.18)


def test_india_intraday_cheaper_than_delivery_and_slippage():
    assert india_intraday().round_trip_pct() < india_delivery().round_trip_pct()
    assert india_intraday(slippage_pct=0.1).round_trip_pct() > india_intraday(slippage_pct=0.0).round_trip_pct()


def test_existing_models_unchanged():
    d = india_delivery()
    assert d.fee(BUY, 1000, 100.0) == pytest.approx(100_000 * (0.001 + (0.0000297 + 1e-6) * 1.18 + 0.0005 + 0.00015))
    assert flat(0.002).fee(SELL, 10, 100.0) == pytest.approx(2.0)
