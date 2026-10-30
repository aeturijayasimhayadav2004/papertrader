"""OrderExecutor: paper / shadow behaviour and the live lock. Offline; no real broker exists."""
from __future__ import annotations

import datetime as dt
import itertools
import json

import pytest

from papertrader.agent.market import IST
from papertrader.broker import Order
from papertrader.live.executor import (DUPLICATE, LIVE_CONFIRM_ENV, LIVE_CONFIRM_PHRASE, PAPER, SENT, SKIPPED,
                                       WOULD_SEND, ExecutorLimits, LiveTradingLocked, OrderExecutor, OrderIntent,
                                       kill_switch_on, make_client_order_id, preflight_problem)
from papertrader.live.interface import BUY, MAX_TAG_LEN, SELL, is_on_tick
from papertrader.live.simulated import SimulatedExchange

NOW = dt.datetime(2026, 10, 29, 10, 0, tzinfo=IST)
DAY = NOW.date()


def clock():
    return NOW


def intent(ticker="INFY.NS", side=BUY, qty=5, strategy="sma_cross", ref=1500.0, **kw):
    return OrderIntent(DAY, ticker, side, qty, strategy, ref, **kw)


def exchange():
    ex = SimulatedExchange(cash=100_000.0)
    ex.set_time(NOW)
    ex.login()
    ex.set_quote("INFY", 1500.0, bid=1499.9, ask=1500.1)
    ex.set_quote("TCS", 3000.0, bid=2999.9, ask=3000.1)
    return ex


class VerifiedFakeAdapter:
    """Test double standing in for a FUTURE verified real adapter. It wraps the offline
    SimulatedExchange, so even with every gate open nothing leaves this process."""
    name = "fake"
    verified = True

    def __init__(self, ex: SimulatedExchange, logged_in: bool = True):
        self.ex, self.logged_in = ex, logged_in

    def session_status(self):
        st = self.ex.session_status()
        return st if self.logged_in else type(st)(False, None, note="expired")

    def __getattr__(self, item):
        return getattr(self.ex, item)


class UnverifiedFakeAdapter(VerifiedFakeAdapter):
    verified = False


def write_preflight(path, passed=True, age_h=1.0):
    path.write_text(json.dumps({"timestamp": (NOW - dt.timedelta(hours=age_h)).isoformat(), "passed": passed,
                                "results": [{"name": "tests", "ok": passed, "detail": ""}]}), encoding="utf-8")
    return path


@pytest.fixture
def paths(tmp_path):
    return {"audit_path": tmp_path / "audit.jsonl", "preflight_path": tmp_path / "preflight.json",
            "reconcile_path": tmp_path / "reconcile.json", "kill_switch_file": tmp_path / "KILL_SWITCH"}


# ----- client order id -----

def test_client_order_id_is_deterministic_and_fits_tag():
    a = make_client_order_id(DAY, "INFY.NS", "BUY", "sma_cross")
    assert a == make_client_order_id("2026-10-29", "INFY", "buy", "sma_cross")
    assert len(a) <= MAX_TAG_LEN and a.startswith("PT")
    others = {make_client_order_id(DAY + dt.timedelta(days=1), "INFY", "BUY", "sma_cross"),
              make_client_order_id(DAY, "TCS", "BUY", "sma_cross"),
              make_client_order_id(DAY, "INFY", "SELL", "sma_cross"),
              make_client_order_id(DAY, "INFY", "BUY", "rsi")}
    assert a not in others and len(others) == 4


def test_intent_from_paper_order():
    o = Order("o1", DAY, "INFY.NS", BUY, 3, "sma_cross", "golden cross")
    it = OrderIntent.from_order(o, ref_price=1500.0)
    assert (it.ticker, it.qty, it.strategy, it.day) == ("INFY.NS", 3, "sma_cross", DAY)


def test_build_request_protected_tick_rounded(paths):
    ex = OrderExecutor("paper", clock=clock, **paths)
    b = ex.build_request(intent(ref=1500.03))
    s = ex.build_request(intent(side=SELL, ref=1500.03))
    assert b.symbol == "INFY" and b.price <= 1500.03 * 1.005 and is_on_tick(b.price)
    assert s.price >= 1500.03 * 0.995 and is_on_tick(s.price)
    assert b.tag == b.client_order_id == intent(ref=1500.03).client_order_id


# ----- paper -----

def test_paper_mode_records_without_broker(paths):
    ex = exchange()
    rep = OrderExecutor("paper", ex, clock=clock, **paths).execute([intent()])
    assert [r.action for r in rep.results] == [PAPER]
    assert ex.place_calls == 0
    assert json.loads(paths["audit_path"].read_text().splitlines()[0])["event"] == PAPER


def test_invalid_mode_rejected(paths):
    with pytest.raises(ValueError):
        OrderExecutor("real", clock=clock, **paths)


# ----- shadow -----

def test_shadow_never_calls_place_order_and_logs_requests(paths):
    ex = exchange()
    ox = OrderExecutor("shadow", ex, clock=clock, **paths)
    rep = ox.execute([intent(), intent("TCS.NS", qty=2, ref=3000.0)], bot_positions={})
    assert [r.action for r in rep.results] == [WOULD_SEND, WOULD_SEND]
    assert ex.place_calls == 0 and ex.orders() == []
    recs = [json.loads(x) for x in paths["audit_path"].read_text().splitlines()]
    sent = [r for r in recs if r["event"] == WOULD_SEND]
    assert sent[0]["request"]["symbol"] == "INFY" and sent[0]["request"]["client_order_id"]
    assert rep.reconciliation is not None and rep.reconciliation.clean
    assert paths["reconcile_path"].exists()


def test_shadow_reconcile_reports_differences(paths):
    ex = SimulatedExchange(cash=50_000, holdings={"INFY": (10, 1400.0)})
    ex.set_time(NOW)
    ex.login()
    rep = OrderExecutor("shadow", ex, clock=clock, **paths).execute([], bot_positions={"INFY.NS": 8, "TCS.NS": 1})
    kinds = {d.kind for d in rep.reconciliation.differences}
    assert {"qty_mismatch", "missing_at_broker"} <= kinds and not rep.reconciliation.clean
    saved = json.loads(paths["reconcile_path"].read_text())
    assert saved["clean"] is False and saved["worst"] == "critical"


def test_retry_does_not_double_send(paths):
    ex = exchange()
    ox = OrderExecutor("shadow", ex, clock=clock, **paths)
    ox.execute([intent()])
    again = OrderExecutor("shadow", ex, clock=clock, **paths).execute([intent()])   # a fresh process retrying
    assert [r.action for r in again.results] == [DUPLICATE]


def test_daily_order_limit_and_notional_limit(paths):
    limits = ExecutorLimits(max_orders_per_day=2, max_notional_per_order=10_000)
    ox = OrderExecutor("shadow", clock=clock, limits=limits, **paths)
    rep = ox.execute([intent(strategy=f"s{i}", qty=1) for i in range(3)] + [intent(qty=100, strategy="big")])
    assert [r.action for r in rep.results] == [WOULD_SEND, WOULD_SEND, SKIPPED, SKIPPED]
    assert "daily order limit" in rep.results[2].reason and "per-order limit" in rep.results[3].reason


def test_invalid_intent_is_skipped(paths):
    rep = OrderExecutor("shadow", clock=clock, **paths).execute([intent(qty=0), intent(ref=0)])
    assert all(r.action == SKIPPED and "invalid" in r.reason for r in rep.results)


# ----- the live lock -----

def test_live_is_locked_today_with_defaults(paths, monkeypatch):
    """With everything as it ships (config paper, no env, no preflight, simulated broker): locked."""
    monkeypatch.delenv(LIVE_CONFIRM_ENV, raising=False)
    with pytest.raises(LiveTradingLocked) as err:
        OrderExecutor("live", exchange(), clock=clock, **paths)
    msg = str(err.value)
    for part in ("trading.mode", LIVE_CONFIRM_ENV, "preflight", "SimulatedExchange"):
        assert part in msg


def test_simulated_exchange_can_never_go_live(paths):
    write_preflight(paths["preflight_path"])
    ex = exchange()
    SimEx = type(ex)
    assert SimEx.verified is False
    with pytest.raises(LiveTradingLocked, match="SimulatedExchange"):
        OrderExecutor("live", ex, config_mode="live", env={LIVE_CONFIRM_ENV: LIVE_CONFIRM_PHRASE},
                      clock=clock, **paths)
    assert ex.place_calls == 0


GATES = ("config", "env", "cap", "kill_off", "preflight", "verified", "logged_in")


def _live_executor(paths, ok: dict[str, bool]):
    ex = exchange()
    if ok["preflight"]:
        write_preflight(paths["preflight_path"])
    elif paths["preflight_path"].exists():
        paths["preflight_path"].unlink()
    if not ok["kill_off"]:
        paths["kill_switch_file"].write_text("stop")
    elif paths["kill_switch_file"].exists():
        paths["kill_switch_file"].unlink()
    cls = VerifiedFakeAdapter if ok["verified"] else UnverifiedFakeAdapter
    adapter = cls(ex, logged_in=ok["logged_in"])
    env = {LIVE_CONFIRM_ENV: LIVE_CONFIRM_PHRASE if ok["env"] else "yes"}
    limits = ExecutorLimits(capital_cap=25_000.0 if ok["cap"] else None)
    return ex, dict(mode="live", session=adapter, config_mode="live" if ok["config"] else "paper",
                    env=env, limits=limits, clock=clock, **paths)


@pytest.mark.parametrize("combo", [c for c in itertools.product([True, False], repeat=len(GATES)) if not all(c)])
def test_live_raises_for_every_unmet_combination(paths, combo):
    ok = dict(zip(GATES, combo))
    ex, kwargs = _live_executor(paths, ok)
    with pytest.raises(LiveTradingLocked):
        OrderExecutor(**kwargs)
    # also when the lock is checked at execute time (conditions can change after construction)
    kwargs_open = {**kwargs, "mode": "shadow"}
    ox = OrderExecutor(**kwargs_open)
    ox.mode = "live"
    with pytest.raises(LiveTradingLocked):
        ox.execute([intent()])
    assert ex.place_calls == 0


def test_all_gates_met_with_a_verified_fake_reaches_the_adapter(paths):
    """Proves the lock is a lock, not a wall: only when EVERY gate holds does an order reach
    the adapter. The adapter here is a test double over the offline SimulatedExchange."""
    ex, kwargs = _live_executor(paths, dict.fromkeys(GATES, True))
    rep = OrderExecutor(**kwargs).execute([intent(qty=2)])
    assert [r.action for r in rep.results] == [SENT] and ex.place_calls == 1
    assert rep.results[0].broker_order.status == "COMPLETE"


def test_live_capital_cap_exceeded_locks(paths):
    ex, kwargs = _live_executor(paths, dict.fromkeys(GATES, True))
    kwargs["limits"] = ExecutorLimits(capital_cap=5_000.0, max_notional_per_order=10_000)
    ox = OrderExecutor(**kwargs)
    with pytest.raises(LiveTradingLocked, match="capital cap"):
        ox.execute([intent(qty=5)])          # 5 x ~1507 = ~7.5k > 5k cap
    assert ex.place_calls == 0


def test_kill_switch_env_locks_live(paths):
    ex, kwargs = _live_executor(paths, dict.fromkeys(GATES, True))
    kwargs["env"] = {**kwargs["env"], "PAPERTRADER_KILL_SWITCH": "1"}
    with pytest.raises(LiveTradingLocked, match="kill switch"):
        OrderExecutor(**kwargs)


def test_live_lock_is_audited(paths):
    with pytest.raises(LiveTradingLocked):
        OrderExecutor("live", clock=clock, env={}, **paths)
    rec = json.loads(paths["audit_path"].read_text().splitlines()[-1])
    assert rec["event"] == "live_locked" and rec["reasons"]


# ----- preflight freshness / kill switch helper -----

def test_preflight_problem_states(tmp_path):
    p = tmp_path / "pf.json"
    assert "no preflight" in preflight_problem(p, NOW)
    write_preflight(p, passed=True, age_h=1)
    assert preflight_problem(p, NOW) == ""
    write_preflight(p, passed=True, age_h=25)
    assert "old" in preflight_problem(p, NOW)
    write_preflight(p, passed=False, age_h=1)
    assert "did not pass" in preflight_problem(p, NOW)
    p.write_text("not json")
    assert preflight_problem(p, NOW)


def test_kill_switch_helper(tmp_path):
    f = tmp_path / "KILL_SWITCH"
    assert not kill_switch_on({}, f)
    assert kill_switch_on({"PAPERTRADER_KILL_SWITCH": "TRUE"}, f)
    f.write_text("x")
    assert kill_switch_on({}, f)
