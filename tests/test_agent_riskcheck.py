from __future__ import annotations

import datetime as dt

import pytest

from papertrader.agent import market
from papertrader.agent.budget import bracket_ticket, size_position
from papertrader.agent.exitplan import plan_trade
from papertrader.agent.riskcheck import RiskLimits, check_ticket

OPEN_TIME = dt.datetime(2026, 10, 29, 10, 15)     # a Thursday


@pytest.fixture
def setup():
    plan = plan_trade("INFY.NS", 1086.05, stop_method="or_mid", range_high=1084.0, range_low=1076.30)
    t = bracket_ticket(plan, size_position(plan, account=100_000, max_loss=50), now=OPEN_TIME)
    return plan, t


def run(plan, t, **kw):
    args = dict(limits=RiskLimits(max_loss_per_trade=50, max_daily_loss=150), account=100_000, cash=100_000,
                trades_today=0, realised_pnl_today=0.0, now=OPEN_TIME, prev_close=1075.0)
    args.update(kw)
    return check_ticket(t, plan, **args)


def test_pass(setup):
    plan, t = setup
    v = run(plan, t)
    assert v.verdict == "PASS", v.reasons
    assert t.status == "risk_passed" and t.risk_reasons == []
    assert {c["name"] for c in v.checks} >= {"mode", "max_loss", "daily_loss", "trades", "reward_risk", "window",
                                            "position", "cash", "circuit_band", "side"}


def by_name(v, name):
    return next(c for c in v.checks if c["name"] == name)


@pytest.mark.parametrize("kw,name", [
    (dict(limits=RiskLimits(max_loss_per_trade=30, max_daily_loss=150)), "max_loss"),
    (dict(realised_pnl_today=-120.0), "daily_loss"),
    (dict(trades_today=3), "trades"),
    (dict(limits=RiskLimits(max_loss_per_trade=50, max_daily_loss=150, min_reward_risk=2.0)), "reward_risk"),
    (dict(now=dt.datetime(2026, 10, 29, 9, 16)), "window"),       # before 09:20
    (dict(now=dt.datetime(2026, 10, 29, 14, 45)), "window"),      # after 14:30
    (dict(now=dt.datetime(2026, 10, 31, 11, 0)), "window"),       # Saturday
    (dict(limits=RiskLimits(max_loss_per_trade=50, max_daily_loss=150, max_position_pct=5)), "position"),
    (dict(cash=3_000), "cash"),
    (dict(prev_close=850.0), "circuit_band"),
])
def test_block_reasons(setup, kw, name):
    plan, t = setup
    v = run(plan, t, **kw)
    assert v.verdict == "BLOCK"
    assert not by_name(v, name)["ok"]
    assert by_name(v, name)["detail"] in v.reasons
    assert t.status == "rejected" and t.risk_reasons == v.reasons


def test_holiday_blocks(setup, monkeypatch):
    plan, t = setup
    monkeypatch.setitem(market.HOLIDAYS, dt.date(2026, 10, 29), "Test holiday")
    v = run(plan, t)
    assert v.verdict == "BLOCK" and "market not open" in by_name(v, "window")["detail"]


def test_live_ticket_blocked(setup):
    plan, t = setup
    t.mode = "live"
    v = run(plan, t)
    assert v.verdict == "BLOCK" and not by_name(v, "mode")["ok"]


def test_reasons_have_rupees_and_ist(setup):
    plan, t = setup
    v = run(plan, t, cash=3_000, now=dt.datetime(2026, 10, 29, 15, 0))
    text = " ".join(v.reasons)
    assert "Rs " in text and "IST" in text


def test_delivery_ticket_skips_intraday_window(setup):
    swing = plan_trade("INFY.NS", 1086.05, stop_method="pct", stop_pct=3, target_r=2, style="swing")
    t = bracket_ticket(swing, size_position(swing, account=1_000_000, max_loss=1000), now=OPEN_TIME)
    v = check_ticket(t, swing, limits=RiskLimits(), account=1_000_000, cash=1_000_000, trades_today=0,
                     realised_pnl_today=0, now=dt.datetime(2026, 10, 29, 20, 0))
    assert v.verdict == "PASS", v.reasons
    assert "Fri 30 Oct" in by_name(v, "window")["detail"]
