from __future__ import annotations

import datetime as dt

import pytest

from papertrader.agent.budget import bracket_ticket, describe_sizing, size_position
from papertrader.agent.exitplan import plan_trade
from papertrader.agent.tickets import LiveTradingRefused
from papertrader.costs import flat


@pytest.fixture
def plan():
    return plan_trade("INFY.NS", 1086.05, stop_method="or_mid", range_high=1084.0, range_low=1076.30)


def test_worked_example_without_costs(plan):
    # max loss Rs 50 / risk Rs 5.90 a share = 8 shares (the US pack's "Buy 8")
    s = size_position(plan, account=100_000, max_loss=50, costs=flat(0.0))
    assert s["qty"] == 8
    assert s["loss_at_stop"] == pytest.approx(47.20)
    assert s["notional"] == pytest.approx(8688.40)
    assert s["binding"] == "max_loss"


def test_charges_cut_the_size_and_loss_stays_within_limit(plan):
    s = size_position(plan, account=100_000, max_loss=50)   # NSE intraday charges
    assert s["qty"] == 6
    assert s["loss_at_stop"] <= 50
    assert s["gain_at_target"] < 6 * 5.90


def test_position_cap_and_cash_caps(plan):
    s = size_position(plan, account=100_000, max_loss=10_000, max_position_pct=20, costs=flat(0.0))
    assert s["qty"] == 18 and s["binding"] == "position"     # 20,000 / 1,086.05
    s = size_position(plan, account=100_000, max_loss=10_000, max_position_pct=100, cash=5_000, costs=flat(0.0))
    assert s["qty"] == 4 and s["binding"] == "cash"


def test_risk_pct_and_smaller_of_two(plan):
    assert size_position(plan, account=100_000, risk_pct=0.5, costs=flat(0.0))["max_loss"] == 500
    assert size_position(plan, account=100_000, risk_pct=0.5, max_loss=50, costs=flat(0.0))["max_loss"] == 50
    assert size_position(plan, account=100_000, costs=flat(0.0))["max_loss"] == 1000   # default 1%


def test_zero_shares_has_reasons(plan):
    s = size_position(plan, account=100_000, max_loss=5)
    assert s["qty"] == 0 and not s["ok"]
    assert "max loss" in s["reasons"][0]
    s = size_position(plan, account=100_000, max_loss=500, cash=100)
    assert any("Not enough cash" in r for r in s["reasons"])
    assert "No trade" in describe_sizing(s)
    with pytest.raises(ValueError):
        bracket_ticket(plan, s)


def test_bracket_ticket(plan):
    s = size_position(plan, account=100_000, max_loss=50)
    t = bracket_ticket(plan, s, now=dt.datetime(2026, 10, 29, 10, 15))
    assert t.ticket_id == "INFY-20261029-101500"
    assert (t.qty, t.limit_price, t.stop_loss, t.target) == (6, 1086.05, 1080.15, 1091.95)
    assert t.product == "MIS" and t.mode == "paper" and t.status == "draft" and t.flat_by == "15:15"
    with pytest.raises(LiveTradingRefused):
        bracket_ticket(plan, s, mode="live")
    swing = plan_trade("INFY.NS", 1086.05, stop_method="pct", stop_pct=5, style="swing")
    assert bracket_ticket(swing, size_position(swing, account=1e6), now=dt.datetime(2026, 10, 29)).product == "CNC"
