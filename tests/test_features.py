"""Tests for costs, exits, sizing, protections, new strategies, walk-forward selection and safety switches."""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from papertrader.backtest import assert_no_lookahead, market_regime, simulate_sleeve
from papertrader.broker import PaperBroker
from papertrader.config import ConfigError, ProtectionConfig, RiskConfig, parse_config
from papertrader.costs import BUY, SELL, flat, india_delivery
from papertrader.exits import ExitRules, intraday_exit
from papertrader.indicators import atr, supertrend
from papertrader.metrics import monte_carlo, profit_factor, sortino
from papertrader.protections import evaluate
from papertrader.risk import RiskManager
from papertrader.selection import _folds, evaluate_strategies
from papertrader.strategies import BollingerReversion, MACrossover, Momentum, Supertrend
from tests.conftest import RAW_CONFIG, make_ohlc, random_walk

D = dt.date


# ---------------------------------------------------------------- costs
def test_india_delivery_charges_match_a_contract_note():
    cm = india_delivery(slippage_pct=0.0, brokerage_pct=0.0)
    # Rs 1,00,000 buy: STT 100 + stamp 15 + exchange 2.97 + SEBI 0.10 + GST 18% on (2.97 + 0.10)
    assert cm.fee(BUY, 1000, 100.0) == pytest.approx(100 + 15 + 2.97 + 0.10 + 0.18 * 3.07, abs=0.01)
    # sell: no stamp duty, plus the DP charge
    assert cm.fee(SELL, 1000, 100.0) == pytest.approx(100 + 2.97 + 0.10 + 0.18 * 3.07 + 15.93, abs=0.01)


def test_brokerage_cap_and_sizing_never_overspends():
    cm = india_delivery(brokerage_pct=0.03, brokerage_cap=20.0)
    assert cm.fee(BUY, 10_000, 100.0) - india_delivery(brokerage_pct=0.0).fee(BUY, 10_000, 100.0) == \
        pytest.approx(20 * 1.18)
    qty = cm.max_qty(50_000, 123.45)
    assert qty * 123.45 + cm.fee(BUY, qty, 123.45) <= 50_000 + 1e-6


def test_flat_model_matches_old_per_side_cost():
    cm = flat(0.002)
    assert cm.fee(BUY, 10, 100) == pytest.approx(2.0)
    assert cm.round_trip_pct() == pytest.approx(0.004)


# ---------------------------------------------------------------- exits
def test_atr_stop_is_never_wider_than_fixed_stop():
    r = ExitRules(stop_loss_pct=5, stop_mode="atr", atr_multiple=2)
    assert r.initial_stop(100, atr_at_signal=1.0) == pytest.approx(98.0)   # 2 x ATR
    assert r.initial_stop(100, atr_at_signal=10.0) == pytest.approx(95.0)  # capped at 5%


def test_trailing_stop_only_moves_up():
    r = ExitRules(stop_loss_pct=5, trailing_pct=10)
    assert r.trail(95, 120) == pytest.approx(108)
    assert r.trail(108, 110) == pytest.approx(108)


def test_intraday_exit_assumes_stop_first_and_gaps_fill_at_open():
    assert intraday_exit(100, 115, 94, stop=95, target=110) == (95, "stop_loss")
    assert intraday_exit(90, 92, 89, stop=95, target=110) == (90, "stop_loss")
    assert intraday_exit(112, 115, 111, stop=95, target=110) == (112, "take_profit")
    assert intraday_exit(100, 105, 96, stop=95, target=110) is None


def test_backtest_take_profit_and_trailing_stop():
    closes = [100, 100, 101, 104, 108, 112, 110, 105, 100]
    df = make_ohlc(closes)
    sig = pd.Series("HOLD", index=df.index)
    sig.iloc[0] = "BUY"
    tp = simulate_sleeve(df, sig, df.index[0], df.index[-1], 10_000, 0.0,
                         exits=ExitRules(stop_loss_pct=5, take_profit_pct=8))
    assert tp.trades[0].exit_reason == "take_profit"
    assert tp.trades[0].exit_price == pytest.approx(108.0)  # entry 100 at day 1 open
    trail = simulate_sleeve(df, sig, df.index[0], df.index[-1], 10_000, 0.0,
                            exits=ExitRules(stop_loss_pct=5, trailing_pct=5))
    t = trail.trades[0]
    assert t.exit_reason == "stop_loss" and t.exit_price == pytest.approx(112 * 0.95)


def test_broker_trailing_stop_and_take_profit_match_backtest_rules():
    b = PaperBroker(100_000, 0.0, 5, ExitRules(stop_loss_pct=5, trailing_pct=5, take_profit_pct=20))
    b.place_order(D(2024, 1, 1), "X", BUY, 10, "s", "r")
    b.fill_pending(D(2024, 1, 2), {"X": 100.0})
    assert b.positions["X"].target_price == pytest.approx(120)
    b.mark({"X": 110.0})
    assert b.positions["X"].stop_price == pytest.approx(104.5)
    trades = b.check_exits(D(2024, 1, 3), {"X": 109.0}, {"X": 109.5}, {"X": 104.0})
    assert trades[0].exit_reason == "stop_loss" and trades[0].exit_price == pytest.approx(104.5)


def test_old_saved_state_without_new_fields_still_loads():
    state = {"cash": 1000.0, "positions": [{
        "ticker": "X", "qty": 1, "entry_date": "2024-01-02", "entry_price": 100.0, "entry_fees": 0.0,
        "stop_price": 95.0, "strategy": "s", "last_price": 101.0}], "pending_orders": []}
    b = PaperBroker.from_dict(state, 0.002, 5)
    assert b.positions["X"].target_price is None
    b.mark({"X": 102.0})
    assert b.positions["X"].stop_price == 95.0


# ---------------------------------------------------------------- sizing
def test_risk_based_sizing_limits_loss_per_trade():
    cfg = RiskConfig(max_position_pct=50, stop_loss_pct=5, max_open_positions=5, max_daily_loss_pct=2,
                     sizing="risk_based", risk_per_trade_pct=1)
    d = RiskManager(cfg, 0.0).check_new_position(price=100, equity=1_000_000, cash_available=1_000_000,
                                                 open_positions=0, pending_buys=0, halted=False)
    # 1% of equity = 10,000 at risk, 5 per share -> 2,000 shares
    assert d.allowed and d.qty == 2000


def test_daily_order_cap():
    cfg = RiskConfig(10, 5, 5, 2, max_new_orders_per_day=2)
    d = RiskManager(cfg, 0.0).check_new_position(price=100, equity=1e6, cash_available=1e6, open_positions=0,
                                                 pending_buys=0, halted=False, orders_today=2)
    assert not d.allowed and "new buy orders today" in d.reason


# ---------------------------------------------------------------- protections
def test_stoploss_guard_pauses_buys_then_expires():
    cfg = ProtectionConfig(stoploss_guard_lookback_days=10, stoploss_guard_max_stops=2, stoploss_guard_pause_days=5)
    trades = [{"ticker": "A", "exit_date": "2024-01-02", "exit_reason": "stop_loss", "return_pct": -0.05},
              {"ticker": "B", "exit_date": "2024-01-05", "exit_reason": "stop_loss", "return_pct": -0.05}]
    assert evaluate(D(2024, 1, 6), cfg, trades, []).blocks("C")
    assert evaluate(D(2024, 1, 10), cfg, trades, []).blocks("C") is None


def test_max_drawdown_protection_window():
    cfg = ProtectionConfig(max_drawdown_pct=10, max_drawdown_lookback_days=30, max_drawdown_pause_days=7)
    hist = [("2024-01-01", 100.0), ("2024-01-10", 88.0)]
    assert "equity fell" in evaluate(D(2024, 1, 12), cfg, [], hist).blocks("X")
    assert evaluate(D(2024, 1, 20), cfg, [], hist).blocks("X") is None


def test_cooldown_and_low_profit_locks_are_per_stock():
    cfg = ProtectionConfig(cooldown_days=3, low_profit_lookback_trades=2, low_profit_lock_days=30)
    trades = [{"ticker": "A", "exit_date": "2024-01-01", "exit_reason": "signal", "return_pct": -0.02},
              {"ticker": "A", "exit_date": "2024-01-05", "exit_reason": "signal", "return_pct": -0.01}]
    st = evaluate(D(2024, 1, 6), cfg, trades, [])
    assert st.blocks("B") is None
    assert "lost" in st.blocks("A")  # low-profit lock outlasts the cooldown
    assert evaluate(D(2024, 2, 10), cfg, trades, []).blocks("A") is None


# ---------------------------------------------------------------- indicators / strategies
@pytest.mark.parametrize("strategy", [Supertrend(), BollingerReversion(trend_window=50), Momentum(lookback=60, trend_window=50)])
def test_new_strategies_have_no_lookahead(strategy):
    assert_no_lookahead(strategy, random_walk(500, seed=3))


def test_supertrend_flips_with_the_trend():
    up = np.linspace(100, 200, 80)
    df = make_ohlc(np.concatenate([up, up[::-1]]))
    _, direction = supertrend(df, 10, 3)
    assert direction.iloc[70] == 1 and direction.iloc[-1] == -1
    sig = Supertrend().signals(df)
    assert (sig == "SELL").sum() >= 1


def test_atr_positive_and_causal():
    df = random_walk(100)
    a = atr(df, 14)
    assert a.iloc[:13].isna().all() and (a.dropna() > 0).all()
    assert atr(df.iloc[:50], 14).iloc[-1] == pytest.approx(a.iloc[49])


def test_market_regime_flags_downtrend():
    bench = make_ohlc(np.concatenate([np.linspace(100, 150, 60), np.linspace(150, 90, 40)]))
    reg = market_regime(bench, 20)
    assert bool(reg.iloc[50]) and not bool(reg.iloc[-1])


def test_regime_filter_blocks_entries_in_backtest():
    df = make_ohlc(np.linspace(100, 110, 30))
    sig = pd.Series("BUY", index=df.index)
    blocked = simulate_sleeve(df, sig, df.index[0], df.index[-1], 10_000, 0.0, regime=pd.Series(False, df.index))
    assert blocked.trades == []


# ---------------------------------------------------------------- metrics
def test_extra_metrics():
    assert profit_factor([0.1, -0.05, 0.05]) == pytest.approx(3.0)
    eq = pd.Series([100, 102, 101, 103.0], index=pd.bdate_range("2024-01-01", periods=4))
    assert sortino(eq, 100) > 0
    mc = monte_carlo([0.05, -0.02, 0.03, -0.01, 0.04, 0.02])
    assert 0 <= mc["prob_loss"] <= 1 and mc["max_drawdown_p95"] <= 0
    assert monte_carlo([0.1]) is None


# ---------------------------------------------------------------- walk-forward
def test_folds_cover_the_period_and_merge_short_tail():
    folds = _folds(D(2022, 1, 1), D(2024, 2, 15), 12)
    assert folds[0][0] == D(2022, 1, 1) and folds[-1][1] == D(2024, 2, 15)
    assert len(folds) == 2  # the 6-week tail is merged


def test_walk_forward_selection_reports_folds(cfg):
    raw = {**RAW_CONFIG, "selection": {**RAW_CONFIG["selection"], "method": "walk_forward", "wf_fold_months": 6},
           "storage": {"sqlite_path": str(cfg.storage.sqlite_path), "use_supabase": False}}
    wf_cfg = parse_config(raw)
    prices = {"AAA.NS": random_walk(700, seed=1), "BBB.NS": random_walk(700, seed=2)}
    as_of = prices["AAA.NS"].index[-1].date()
    sel = evaluate_strategies([MACrossover(5, 20)], prices, wf_cfg, as_of)
    ev = sel.evaluations[0]
    assert ev.fold_win_rate is not None and any("Walk-forward" in n for n in ev.notes)
    rows = sel.to_rows()
    assert {"sortino", "calmar", "profit_factor", "fold_win_rate"} <= set(rows[0])


# ---------------------------------------------------------------- config / safety
def test_live_mode_is_refused():
    with pytest.raises(ConfigError, match="only 'paper'"):
        parse_config({**RAW_CONFIG, "trading": {"mode": "live"}})


def test_bad_cost_model_option_is_a_config_error():
    with pytest.raises(ConfigError):
        parse_config({**RAW_CONFIG, "costs": {"model": "india_delivery", "india": {"bogus": 1}}})
