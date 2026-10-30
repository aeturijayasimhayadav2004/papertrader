"""SimulatedExchange and interface helpers: offline, no network, no real broker."""
from __future__ import annotations

import datetime as dt

import pytest

from papertrader.costs import india_delivery, india_intraday
from papertrader.live.interface import (BUY, CANCELLED, CNC, COMPLETE, LIMIT, MARKET, MIS, OPEN, PARTIAL, REJECTED,
                                        SELL, SL, SL_M, BrokerSession, InvalidOrder, OrderRequest, SessionExpired,
                                        is_on_tick, round_to_tick, within_band)
from papertrader.live.simulated import SimulatedExchange

THU = dt.datetime(2026, 10, 29, 10, 0)      # a normal NSE trading day (IST)


@pytest.fixture
def ex():
    e = SimulatedExchange(cash=100_000.0)
    e.set_time(THU)
    e.login()
    e.set_quote("INFY", 1500.0, bid=1499.9, ask=1500.1, upper=1650.0, lower=1350.0)
    return e


def buy(qty=10, price=1500.5, **kw):
    return OrderRequest("INFY", BUY, qty, price=price, **kw)


# ----- interface helpers -----

def test_tick_rounding():
    assert round_to_tick(100.12) == 100.10
    assert round_to_tick(100.13) == 100.15
    assert round_to_tick(100.14, side=BUY) == 100.10
    assert round_to_tick(100.11, side=SELL) == 100.15
    assert round_to_tick(100.15, side=BUY) == 100.15      # float noise must not push it down a tick
    assert is_on_tick(100.15) and not is_on_tick(100.12)
    with pytest.raises(InvalidOrder):
        round_to_tick(0)


def test_band_and_request_validation():
    assert within_band(100, 90, 110) and not within_band(111, 90, 110) and within_band(5, None, None)
    assert OrderRequest("INFY.NS", BUY, 1, price=10.0).symbol == "INFY"
    bad = OrderRequest("INFY", "HOLD", 0, product="NRML", order_type=SL, price=10.02, tag="x" * 21)
    issues = " ".join(bad.problems())
    for word in ("side", "qty", "product", "trigger_price", "tick", "tag"):
        assert word in issues
    with pytest.raises(InvalidOrder):
        OrderRequest("INFY", BUY, 1, order_type=MARKET, price=10.0).validate()
    OrderRequest("INFY", BUY, 1, order_type=MARKET).validate()


def test_simulated_implements_protocol(ex):
    assert isinstance(ex, BrokerSession)
    assert SimulatedExchange.verified is False


# ----- fills -----

def test_limit_buy_fills_only_when_crossed(ex):
    o = ex.place_order(buy(price=1499.0))
    assert o.status == OPEN and o.filled_qty == 0
    ex.set_quote("INFY", 1498.5, bid=1498.4, ask=1498.9)
    assert o.status == COMPLETE and o.avg_price == 1498.9 and o.filled_qty == 10


def test_limit_sell_fills_only_when_crossed(ex):
    ex.place_order(buy())
    o = ex.place_order(OrderRequest("INFY", SELL, 10, price=1510.0))
    assert o.status == OPEN
    ex.set_quote("INFY", 1511.0, bid=1510.5, ask=1511.0)
    assert o.status == COMPLETE and o.avg_price == 1510.5


def test_market_order_slippage(ex):
    o = ex.place_order(OrderRequest("INFY", BUY, 10, order_type=MARKET))
    assert o.status == COMPLETE
    assert o.avg_price == round_to_tick(1500.1 * 1.0005) and o.avg_price > 1500.1


def test_market_buy_capped_at_upper_circuit(ex):
    ex.set_quote("INFY", 1650.0, bid=1650.0, ask=1650.0, upper=1650.0, lower=1350.0)
    o = ex.place_order(OrderRequest("INFY", BUY, 1, order_type=MARKET))
    assert o.avg_price == 1650.0


def test_partial_fill_then_complete(ex):
    ex.set_quote("INFY", 1500.0, bid=1499.9, ask=1500.1, upper=1650.0, lower=1350.0, liquidity=4)
    o = ex.place_order(buy(qty=10))
    assert o.status == PARTIAL and o.filled_qty == 4 and o.pending_qty == 6
    ex.set_quote("INFY", 1500.0, bid=1499.9, ask=1500.1, liquidity=None)
    assert o.status == COMPLETE and o.filled_qty == 10


def test_stop_orders_wait_for_trigger(ex):
    ex.place_order(buy(qty=10))
    slm = ex.place_order(OrderRequest("INFY", SELL, 5, order_type=SL_M, trigger_price=1450.0))
    sl = ex.place_order(OrderRequest("INFY", SELL, 5, order_type=SL, trigger_price=1440.0, price=1435.0))
    assert slm.status == OPEN and sl.status == OPEN
    ex.set_quote("INFY", 1449.0, bid=1448.9, ask=1449.1)
    assert slm.status == COMPLETE and sl.status == OPEN
    ex.set_quote("INFY", 1438.0, bid=1437.0, ask=1438.5)
    assert sl.status == COMPLETE and sl.avg_price == 1437.0


def test_charges_use_india_cost_models(ex):
    o = ex.place_order(buy())
    expected = india_delivery(slippage_pct=0.0).fee(BUY, 10, o.avg_price)
    assert o.fees == pytest.approx(expected, abs=0.01)
    assert ex.funds().total == pytest.approx(100_000 - 10 * o.avg_price - o.fees, abs=0.02)
    m = ex.place_order(OrderRequest("INFY", BUY, 2, product=MIS, order_type=MARKET))
    assert m.fees == pytest.approx(india_intraday(slippage_pct=0.0).fee(BUY, 2, m.avg_price), abs=0.01)


def test_cancel_releases_blocked_funds(ex):
    o = ex.place_order(buy(price=1400.0))
    assert ex.funds().used_margin > 0
    ex.cancel_order(o.order_id)
    assert o.status == CANCELLED and ex.funds().used_margin == 0
    with pytest.raises(InvalidOrder):
        ex.cancel_order(o.order_id)


# ----- rejections -----

def test_reject_insufficient_funds(ex):
    o = ex.place_order(buy(qty=100))
    assert o.status == REJECTED and "insufficient funds" in o.reject_reason


def test_reject_outside_circuit_band(ex):
    o = ex.place_order(buy(price=1700.0))
    assert o.status == REJECTED and "circuit" in o.reject_reason


def test_reject_invalid_tick(ex):
    o = ex.place_order(buy(price=1500.02))
    assert o.status == REJECTED and "tick" in o.reject_reason


def test_reject_cnc_sell_without_holdings(ex):
    o = ex.place_order(OrderRequest("INFY", SELL, 5, price=1499.0))
    assert o.status == REJECTED and "short" in o.reject_reason


@pytest.mark.parametrize("when", [dt.datetime(2026, 10, 29, 9, 5), dt.datetime(2026, 10, 29, 15, 45),
                                  dt.datetime(2026, 10, 31, 11, 0),          # Saturday
                                  dt.datetime(2026, 11, 10, 11, 0)])         # NSE holiday
def test_reject_outside_market_hours(when):
    e = SimulatedExchange()
    e.set_time(when)
    e.login()
    e.set_quote("INFY", 1500.0)
    o = e.place_order(buy())
    assert o.status == REJECTED and "market closed" in o.reject_reason


def test_mis_rejected_after_square_off_time(ex):
    ex.set_time(dt.datetime(2026, 10, 29, 15, 21))
    o = ex.place_order(OrderRequest("INFY", BUY, 1, product=MIS, order_type=MARKET))
    assert o.status == REJECTED and "MIS" in o.reject_reason
    ok = ex.place_order(OrderRequest("INFY", BUY, 1, product=CNC, order_type=MARKET))
    assert ok.status == COMPLETE


def test_rate_limit_more_than_10_per_second(ex):
    results = [ex.place_order(buy(qty=1, price=1400.0, client_order_id=f"c{i}")) for i in range(11)]
    assert all(o.status == OPEN for o in results[:10])
    assert results[10].status == REJECTED and "rate limit" in results[10].reject_reason
    ex.set_time(THU + dt.timedelta(seconds=2))
    assert ex.place_order(buy(qty=1, price=1400.0, client_order_id="later")).status == OPEN


# ----- idempotency, square-off, expiry, session -----

def test_duplicate_client_order_id_returns_existing(ex):
    a = ex.place_order(buy(client_order_id="PTABC"))
    b = ex.place_order(buy(client_order_id="PTABC"))
    assert a is b and len(ex.orders()) == 1
    assert ex.funds().total == pytest.approx(100_000 - 10 * a.avg_price - a.fees, abs=0.02)


def test_mis_auto_square_off_at_1520(ex):
    ex.place_order(OrderRequest("INFY", BUY, 5, product=MIS, order_type=MARKET))
    resting = ex.place_order(OrderRequest("INFY", BUY, 5, product=MIS, price=1400.0))
    ex.set_quote("INFY", 1510.0, bid=1509.9, ask=1510.1)
    ex.set_time(dt.datetime(2026, 10, 29, 15, 20))
    mis = [p for p in ex.positions() if p.product == MIS]
    assert mis and mis[0].qty == 0 and mis[0].realised_pnl > 0
    assert resting.status == CANCELLED
    assert any(o.request.tag == "AUTO-SQOFF" and o.status == COMPLETE for o in ex.orders())


def test_day_orders_expire_at_close_and_cnc_settles_next_day(ex):
    ex.place_order(buy())
    resting = ex.place_order(OrderRequest("INFY", BUY, 1, price=1400.0))
    ex.set_time(dt.datetime(2026, 10, 29, 15, 31))
    assert resting.status == CANCELLED and "validity DAY" in resting.reject_reason
    assert ex.holdings() == []                              # today's CNC buy is a position, not a holding
    ex.set_time(dt.datetime(2026, 10, 30, 9, 30))
    ex.login()
    h = ex.holdings()
    assert len(h) == 1 and h[0].qty == 10 and ex.positions() == []


def test_session_expires_daily(ex):
    st = ex.session_status()
    assert st.logged_in and st.expires_at == dt.datetime(2026, 10, 30, 6, 0, tzinfo=st.expires_at.tzinfo)
    ex.set_time(dt.datetime(2026, 10, 30, 6, 0, 1))
    assert not ex.session_status().logged_in
    for call in (ex.funds, ex.holdings, ex.positions, ex.orders, lambda: ex.quote(["INFY"]),
                 lambda: ex.place_order(buy())):
        with pytest.raises(SessionExpired):
            call()
    ex.login()
    assert ex.session_status().logged_in


def test_never_logged_in_is_refused():
    e = SimulatedExchange()
    e.set_time(THU)
    with pytest.raises(SessionExpired):
        e.funds()


def test_quote_returns_band(ex):
    q = ex.quote(["INFY.NS", "TCS"])
    assert set(q) == {"INFY"} and q["INFY"].upper_circuit == 1650.0 and q["INFY"].ltp == 1500.0
    assert LIMIT == "LIMIT"
