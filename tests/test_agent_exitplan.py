from __future__ import annotations

import datetime as dt

import pytest

from papertrader.agent.exitplan import PlanError, TradePlan, describe_plan, plan_trade, tick_down, tick_up


def test_orb_worked_example_or_mid_1r():
    # TSLA example from the US pack, adapted: INFY opening range 1,076.30-1,084.00, entry 1,086.05
    p = plan_trade("INFY.NS", 1086.05, stop_method="or_mid", range_high=1084.0, range_low=1076.30)
    assert p.stop == 1080.15            # midpoint 1,080.15
    assert p.risk_per_share == 5.90
    assert p.target == 1091.95          # 1R
    assert p.r_multiple == 1.0
    assert p.flat_by == dt.time(15, 15)
    assert p.side == "long" and p.style == "intraday"


def test_tick_rounding_is_conservative():
    assert tick_down(990.99) == 990.95
    assert tick_up(1000.02) == 1000.05
    assert tick_down(1080.15) == 1080.15     # already on the tick: unchanged (no float drift)
    p = plan_trade("X", 1000.02, stop_method="pct", stop_pct=1.0, target_r=2.0)
    assert p.entry == 1000.05
    assert p.stop == 990.0                   # 990.0495 rounded down
    assert p.target == 1020.15               # 1000.05 + 2 x 10.05 = 1020.15
    assert p.r_multiple <= 2.0


def test_stop_methods():
    assert plan_trade("X", 500, stop_method="atr", atr=5, atr_multiple=2).stop == 490.0
    assert plan_trade("X", 500, stop_method="or_low", range_high=499, range_low=493.33).stop == 493.30
    assert plan_trade("X", 500, stop_method="price", stop_price=480.07).stop == 480.05


@pytest.mark.parametrize("kwargs", [
    dict(stop_method="price", stop_price=500),            # stop at the entry
    dict(stop_method="price", stop_price=510),            # above
    dict(stop_method="pct"),                              # no pct
    dict(stop_method="or_mid", range_high=None, range_low=490),
    dict(stop_method="atr", atr=None),
    dict(stop_method="price", stop_price=499.95),         # 0.01% risk: inside the spread
    dict(stop_method="nonsense"),
])
def test_bad_plans_refused(kwargs):
    with pytest.raises(PlanError):
        plan_trade("X", 500, **kwargs)


def test_swing_has_no_flat_by_and_roundtrip_dict():
    p = plan_trade("TCS.NS", 3500, stop_method="pct", stop_pct=5, target_r=2, style="swing")
    assert p.flat_by is None
    q = plan_trade("TCS.NS", 3500, stop_method="pct", stop_pct=1)
    d = q.to_dict()
    assert d["flat_by"] == "15:15"
    assert TradePlan.from_dict(d) == q
    assert "Rs 3,500.00" in describe_plan(q) and "15:15 IST" in describe_plan(q)
