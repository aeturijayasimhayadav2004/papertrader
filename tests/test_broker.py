import datetime as dt

import pytest

from papertrader.broker import BUY, CANCELLED, FILLED, PENDING, SELL, BrokerError, PaperBroker, Position

D1, D2, D3, D4 = (dt.date(2024, 3, d) for d in (4, 5, 6, 7))
COST = 0.002


@pytest.fixture
def b():
    return PaperBroker(cash=100_000, cost_per_side=COST, stop_loss_pct=5)


def test_order_fills_at_next_days_open_never_same_day(b):
    b.place_order(D1, "X", BUY, 10, "s", "why")
    assert b.fill_pending(D1, {"X": 100}) == []  # same day: no fill
    assert b.pending_orders[0].status == PENDING
    (o,) = b.fill_pending(D2, {"X": 101})
    assert o.status == FILLED and o.fill_date == D2 and o.fill_price == 101
    pos = b.positions["X"]
    assert pos.qty == 10 and pos.entry_price == 101
    assert pos.stop_price == pytest.approx(101 * 0.95)
    assert b.cash == pytest.approx(100_000 - 10 * 101 * (1 + COST))


def test_order_waits_when_no_price_for_that_day(b):
    b.place_order(D1, "X", BUY, 10, "s", "why")
    assert b.fill_pending(D2, {"Y": 50}) == []
    assert len(b.pending_orders) == 1


def test_round_trip_pnl_and_costs(b):
    b.place_order(D1, "X", BUY, 10, "s", "why")
    b.fill_pending(D2, {"X": 100})
    b.place_order(D2, "X", SELL, 10, "s", "exit")
    b.fill_pending(D3, {"X": 110})
    (t,) = b.trades
    assert t.gross_pnl == pytest.approx(100)
    assert t.fees == pytest.approx(10 * 100 * COST + 10 * 110 * COST)
    assert t.net_pnl == pytest.approx(100 - t.fees)
    assert b.cash == pytest.approx(100_000 + t.net_pnl)
    assert "X" not in b.positions


def test_buy_reduced_when_cash_short_and_cancelled_when_none(b):
    b.place_order(D1, "X", BUY, 2_000, "s", "why")
    (o,) = b.fill_pending(D2, {"X": 100})
    assert o.status == FILLED and o.qty == int(100_000 // (100 * (1 + COST)))
    assert "cut" in o.note
    b.place_order(D2, "Y", BUY, 10, "s", "why")
    (o2,) = b.fill_pending(D3, {"Y": 100})
    assert o2.status == CANCELLED and "cash" in o2.note


def test_max_notional_caps_quantity_on_gap_up(b):
    b.place_order(D1, "X", BUY, 100, "s", "why", max_notional=10_000)
    (o,) = b.fill_pending(D2, {"X": 150})  # gapped up from ~100
    assert o.qty == int(10_000 // (150 * (1 + COST)))


def test_stop_loss_fills_at_stop_or_gap_open(b):
    b.place_order(D1, "X", BUY, 10, "s", "why")
    b.place_order(D1, "Y", BUY, 10, "s", "why")
    b.fill_pending(D2, {"X": 100, "Y": 100})
    trades = b.check_stops(D3, opens={"X": 99, "Y": 90}, lows={"X": 94, "Y": 89})
    by = {t.ticker: t for t in trades}
    assert by["X"].exit_price == pytest.approx(95) and by["X"].exit_reason == "stop_loss"
    assert by["Y"].exit_price == 90  # gapped below the stop
    assert not b.positions
    assert {o.order_id for o in b.orders if o.status == FILLED and o.side == SELL} == {
        "20240306-X-STOP", "20240306-Y-STOP"}


def test_no_stop_when_low_stays_above(b):
    b.place_order(D1, "X", BUY, 10, "s", "why")
    b.fill_pending(D2, {"X": 100})
    assert b.check_stops(D3, {"X": 99}, {"X": 95.5}) == []


def test_invalid_orders_rejected(b):
    with pytest.raises(BrokerError):
        b.place_order(D1, "X", SELL, 1, "s", "not held")
    b.place_order(D1, "X", BUY, 1, "s", "why")
    with pytest.raises(BrokerError):
        b.place_order(D1, "X", BUY, 1, "s", "dup")
    with pytest.raises(BrokerError):
        b.place_order(D1, "Z", BUY, 0, "s", "zero")


def test_sells_fill_before_buys_to_free_cash():
    b = PaperBroker(cash=0, cost_per_side=0.0, stop_loss_pct=5)
    b.positions["X"] = Position("X", 10, D1, 100, 0, 95, "s", 100)
    b.place_order(D1, "Y", BUY, 5, "s", "buy")
    b.place_order(D1, "X", SELL, 10, "s", "sell")
    b.fill_pending(D2, {"X": 100, "Y": 100})
    assert "Y" in b.positions and b.positions["Y"].qty == 5


def test_equity_and_state_round_trip(b):
    b.place_order(D1, "X", BUY, 10, "s", "why")
    b.fill_pending(D2, {"X": 100})
    b.place_order(D2, "Y", BUY, 5, "s", "why", max_notional=1_000)
    b.mark({"X": 120})
    assert b.equity() == pytest.approx(b.cash + 1_200)
    restored = PaperBroker.from_dict(b.to_dict(), COST, 5)
    assert restored.cash == pytest.approx(b.cash)
    assert restored.positions == b.positions
    assert [o.order_id for o in restored.pending_orders] == ["20240305-Y-BUY"]
    assert restored.equity() == pytest.approx(b.equity())


def test_drain_touched_reports_changes_once(b):
    b.place_order(D1, "X", BUY, 10, "s", "why")
    assert len(b.drain_touched()) == 1
    assert b.drain_touched() == []
    b.fill_pending(D2, {"X": 100})
    assert [o.status for o in b.drain_touched()] == [FILLED]
