"""Regression tests for the full-project review fixes (splits, stale state, run overlap, late runs,
forced re-runs, half-published bars, rotation edge cases)."""
import dataclasses
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from papertrader.broker import PaperBroker
from papertrader.costs import flat, split_ratio
from papertrader.data import complete_calendar
from papertrader.portfolio.paper import Account, fill_pending
from papertrader.storage import SQLiteStore, StateUnavailableError, Store
from tests.conftest import make_ohlc
from tests.test_runner import IST, runner


def test_split_ratio_detects_bonus_and_split_but_not_small_moves():
    assert split_ratio(1000.0, 500.0) == 0.5            # 1:1 bonus
    assert split_ratio(1000.0, 200.2) == pytest.approx(0.2)   # 1:5 split (snapped)
    assert split_ratio(1000.0, 990.0) is None            # dividend adjustment
    assert split_ratio(None, 500.0) is None


def test_broker_rescales_position_keeping_value():
    b = PaperBroker(1_000_000, flat(0.0), 5.0)
    b.place_order(dt.date(2024, 1, 1), "X", "BUY", 100, "s", "r")
    b.fill_pending(dt.date(2024, 1, 2), {"X": 1000.0})
    b.mark({"X": 1000.0})
    value = b.positions["X"].market_value
    b.apply_split("X", 0.5)
    p = b.positions["X"]
    assert p.qty == 200 and p.entry_price == 500.0 and p.stop_price == pytest.approx(475.0)
    assert p.market_value == pytest.approx(value)
    # next day opens at 501 after the bonus: no fake stop-out
    assert b.check_exits(dt.date(2024, 1, 3), {"X": 501.0}, {"X": 505.0}, {"X": 498.0}) == []


def test_runner_handles_a_bonus_without_a_stop_out(cfg):
    store = Store(SQLiteStore(cfg.storage.sqlite_path))
    runner(cfg, store, 10).run()
    runner(cfg, store, 11).run()                         # AAA bought at the 11th bar's open
    pre = {r["ticker"]: r for r in store.sqlite.rows("positions")}
    assert "AAA.NS" in pre
    base = make_ohlc(np.linspace(100, 120, 40), start="2024-01-01")
    halved = base.copy()
    halved[["Open", "High", "Low", "Close"]] /= 2        # Yahoo rescales history after a 1:1 bonus

    def load(tickers, c, refresh=False, now=None):
        out = {"AAA.NS": halved.iloc[:12], "BBB.NS": make_ohlc(np.linspace(200, 210, 40), start="2024-01-01").iloc[:12],
               "^NSEI": make_ohlc(np.linspace(20000, 21000, 40), start="2024-01-01").iloc[:12]}
        return {t: out[t] for t in tickers if t in out}, []
    r = runner(cfg, store, 12)
    r.price_loader = load
    s = r.run()
    assert any("Corporate action on AAA.NS" in e for e in s.events)
    pos = {r["ticker"]: r for r in store.sqlite.rows("positions")}
    assert pos["AAA.NS"]["qty"] == 2 * pre["AAA.NS"]["qty"]
    assert not store.sqlite.rows("trades")              # no fake stop-loss


class _DownRemote:
    def apply(self, ops):
        raise ConnectionError("down")

    def latest_state(self, before=None):
        raise ConnectionError("down")

    def running_since(self, since):
        return [{"run_id": "other", "host": "oracle"}]


def test_required_supabase_unreachable_refuses_even_with_local_state(tmp_path):
    sq = SQLiteStore(tmp_path / "x.db")
    sq.apply([__import__("papertrader.storage", fromlist=["upsert"]).upsert("daily_snapshots", [{
        "as_of_date": dt.date(2024, 1, 2), "equity": 1.0, "state_json": {"x": 1}}])])
    with pytest.raises(StateUnavailableError):
        Store(sq, _DownRemote(), require_remote=True).load_latest_state()
    assert Store(sq, _DownRemote(), require_remote=False).load_latest_state()[0] == dt.date(2024, 1, 2)


def test_second_runner_does_not_start_while_another_is_running(cfg):
    store = Store(SQLiteStore(cfg.storage.sqlite_path), _DownRemote())
    s = runner(cfg, store, 10).run()
    assert s.status == "noop" and "Another run is in progress" in s.message


def test_late_run_skips_agent_desks(cfg):
    from tests.test_agent_orchestrator import coordinator, desk
    calls = []
    store = Store(SQLiteStore(cfg.storage.sqlite_path))
    r = runner(cfg, store, 10)                           # clock: June 2024, long after the Jan data
    r.coordinator = coordinator({"news": desk("info", calls=calls)})
    r.run()
    assert calls == [] and store.sqlite.count("agent_briefs") == 0


def test_forced_rerun_removes_rows_the_recompute_no_longer_makes(cfg):
    store = Store(SQLiteStore(cfg.storage.sqlite_path))
    runner(cfg, store, 10).run()
    assert store.sqlite.count("orders") == 2
    runner(cfg, store, 10, selected=()).run(force=True)  # nothing selected now: no BUY orders
    assert store.sqlite.count("orders") == 0


def test_half_published_latest_bar_is_not_processed():
    full = make_ohlc(np.linspace(100, 110, 5), start="2024-01-01")
    frames = {f"T{i}": full for i in range(9)} | {"LATE": full.iloc[:4]}
    assert complete_calendar(frames)[-1] == full.index[-1]      # 9/10 have it: complete
    frames = {f"T{i}": full.iloc[:4] for i in range(8)} | {"A": full, "B": full}
    assert complete_calendar(frames)[-1] == full.index[-2]      # 2/10 have it: wait


def test_rotation_keeps_orders_for_a_stock_without_a_bar():
    acct = Account(cash=10_000.0, pending=[["A", "BUY", 5], ["B", "BUY", 5]])
    fill_pending(acct, {"A": 100.0}, flat(0.0), dt.date(2024, 1, 2))
    assert acct.holdings == {"A": 5} and acct.pending == [["B", "BUY", 5]]


def test_config_rejects_bad_rerank_interval(cfg):
    from papertrader.config import ConfigError, parse_config
    from tests.conftest import RAW_CONFIG
    raw = {**RAW_CONFIG, "selection": {**RAW_CONFIG["selection"], "rerank_every_days": 0}}
    with pytest.raises(ConfigError):
        parse_config(raw)
