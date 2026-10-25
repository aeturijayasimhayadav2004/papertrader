import pytest

from papertrader.config import RiskConfig
from papertrader.risk import RiskManager

CFG = RiskConfig(max_position_pct=10, stop_loss_pct=5, max_open_positions=5, max_daily_loss_pct=2)


@pytest.fixture
def rm():
    return RiskManager(CFG, cost_per_side=0.002)


def check(rm, **kw):
    args = dict(price=100.0, equity=1_000_000, cash_available=1_000_000, open_positions=0, pending_buys=0,
                halted=False)
    args.update(kw)
    return rm.check_new_position(**args)


def test_position_size_capped_at_10_percent_of_equity(rm):
    d = check(rm)
    assert d.allowed
    assert d.qty == int(100_000 // (100 * 1.002))
    assert d.qty * 100 * 1.002 <= 100_000
    assert d.max_notional == pytest.approx(100_000)


def test_size_limited_by_free_cash(rm):
    d = check(rm, cash_available=50_000)
    assert d.allowed and d.qty == int(50_000 // (100 * 1.002))
    assert "free cash" in d.reason


def test_max_open_positions_counts_queued_buys(rm):
    assert not check(rm, open_positions=5).allowed
    d = check(rm, open_positions=3, pending_buys=2)
    assert not d.allowed and "limit of 5" in d.reason
    assert check(rm, open_positions=4).allowed


def test_halt_blocks_new_buys(rm):
    d = check(rm, halted=True, halt_reason="lost 2.5% today")
    assert not d.allowed and d.reason == "lost 2.5% today"


def test_share_too_expensive_or_no_cash(rm):
    assert "costs more than" in check(rm, price=150_000).reason
    assert "Not enough free cash" in check(rm, cash_available=50).reason


def test_daily_loss_limit(rm):
    halted, change, reason = rm.daily_loss(1_000_000, 979_000)
    assert halted and change == pytest.approx(-0.021) and "2%" in reason
    assert rm.daily_loss(1_000_000, 980_001)[0] is False
    assert rm.daily_loss(1_000_000, 980_000)[0] is True  # exactly -2% halts
    assert rm.daily_loss(1_000_000, 1_050_000)[0] is False


def test_stop_price(rm):
    assert rm.stop_price(200) == pytest.approx(190)
