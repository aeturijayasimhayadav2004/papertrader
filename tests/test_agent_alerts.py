from __future__ import annotations

import datetime as dt
import json

import pytest

from papertrader.agent import alerts
from papertrader.agent.exitplan import plan_trade

DAY = dt.date(2026, 10, 29)


def at(h, m):
    return dt.datetime(2026, 10, 29, h, m)


@pytest.fixture
def plan():
    return plan_trade("INFY.NS", 1086.05, stop_method="or_mid", range_high=1084.0, range_low=1076.30)


def test_rules_from_plan(plan):
    rules = alerts.rules_from_plan(plan, DAY)
    assert [(r["type"], r.get("price"), r.get("at")) for r in rules] == [
        ("stop", 1080.15, None), ("target", 1091.95, None), ("time", None, "15:15")]
    assert all(r["date"] == "2026-10-29" and r["ticker"] == "INFY.NS" for r in rules)


def test_rings_once(plan):
    rules = alerts.rules_from_plan(plan, DAY)
    state: dict = {}
    assert alerts.check(rules, {"INFY.NS": {"price": 1085.0, "prev_close": 1075.0}}, at(10, 30), state) == []
    fired = alerts.check(rules, {"INFY.NS": {"price": 1079.9, "prev_close": 1075.0}}, at(10, 35), state)
    assert [f["id"] for f in fired] == ["infy-plan-stop"]
    assert "Rs 1,079.90" in fired[0]["message"]
    assert alerts.check(rules, {"INFY.NS": {"price": 1079.0, "prev_close": 1075.0}}, at(10, 40), state) == []
    fired = alerts.check(rules, {"INFY.NS": {"price": 1079.0}}, at(15, 16), state)
    assert [f["id"] for f in fired] == ["infy-flat-by"]
    assert alerts.check(rules, {"INFY.NS": {"price": 1079.0}}, at(15, 20), state) == []


def test_dated_rules_only_on_their_day(plan):
    rules = alerts.rules_from_plan(plan, DAY)
    nxt = dt.datetime(2026, 10, 30, 10, 0)
    assert alerts.check(rules, {"INFY.NS": {"price": 1.0}}, nxt, {}) == []


def test_pct_move_and_opening_bell_daily():
    rules: list = []
    alerts.add_rule(rules, "pct_move", ticker="TCS", pct=3, direction="down")
    alerts.add_rule(rules, "opening_bell")
    alerts.add_rule(rules, "above", ticker="TCS", price=4000)
    state: dict = {}
    q = {"TCS.NS": {"price": 3880.0, "prev_close": 4000.0}}
    assert [f["type"] for f in alerts.check(rules, q, at(9, 0), state)] == ["pct_move"]   # bell not yet
    assert [f["type"] for f in alerts.check(rules, q, at(9, 20), state)] == ["opening_bell"]
    assert alerts.check(rules, q, at(9, 30), state) == []
    fired = alerts.check(rules, q, dt.datetime(2026, 10, 30, 9, 20), state)     # next day: daily ones again
    assert sorted(f["type"] for f in fired) == ["opening_bell", "pct_move"]
    up = alerts.check(rules, {"TCS.NS": {"price": 4100.0, "prev_close": 4000.0}}, at(11, 0), {})
    assert [f["type"] for f in up] == ["opening_bell", "above"]                # 'down' move rule stays quiet
    assert alerts.check(rules, q, dt.datetime(2026, 10, 31, 10, 0), {}) == []  # Saturday


def test_add_rule_validation():
    with pytest.raises(ValueError):
        alerts.add_rule([], "stop", ticker="INFY")
    with pytest.raises(ValueError):
        alerts.add_rule([], "time", at="25:00")
    with pytest.raises(ValueError):
        alerts.add_rule([], "buy", ticker="INFY")
    rules: list = []
    alerts.add_rule(rules, "below", ticker="INFY", price=1000, rule_id="x")
    alerts.add_rule(rules, "below", ticker="INFY", price=990, rule_id="x")
    assert len(rules) == 1 and rules[0]["price"] == 990


def test_store_roundtrip(tmp_path, plan):
    rules = alerts.rules_from_plan(plan, DAY)
    alerts.save_rules(tmp_path / "a.json", rules)
    assert alerts.load_rules(tmp_path / "a.json") == rules
    state = {"fired": {"x": "t"}}
    alerts.save_state(tmp_path / "s.json", state)
    assert alerts.load_state(tmp_path / "s.json") == state
    assert alerts.load_state(tmp_path / "missing.json") == {"fired": {}}


def test_fetch_quotes_offline_skips_missing():
    def fake(url: str) -> bytes:
        assert "interval=1m&range=1d" in url
        if "INFY.NS" in url:
            return json.dumps({"chart": {"result": [{"meta": {"regularMarketPrice": 1146.5, "previousClose": 1123.15,
                                                              "regularMarketTime": 1793267942}}]}}).encode()
        if "BAD.NS" in url:
            return b"not json"
        raise OSError("404")
    q = alerts.fetch_quotes(["INFY", "ZZZZ.NS", "BAD"], fetch=fake)
    assert list(q) == ["INFY.NS"]
    assert q["INFY.NS"]["price"] == 1146.5 and q["INFY.NS"]["prev_close"] == 1123.15


def test_send_information_only():
    sent = []
    assert alerts.send([], notifier=lambda t: sent.append(t) or True) is False and sent == []
    fired = [{"message": "INFY at Rs 1,079.90, at or below Rs 1,080.15"}]
    assert alerts.send(fired, notifier=lambda t: sent.append(t) or True) is True
    assert "information only, not orders" in sent[0] and "PAPER MONEY" in sent[0]

    def boom(_):
        raise RuntimeError("down")
    assert alerts.send(fired, notifier=boom) is False
