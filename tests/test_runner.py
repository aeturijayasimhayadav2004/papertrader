"""End-to-end runner tests with synthetic prices and a local SQLite store."""
import datetime as dt
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from papertrader.runner import DailyRunner
from papertrader.selection import Selection
from papertrader.storage import SQLiteStore, StateUnavailableError, Store
from papertrader.storage.schema import TABLES
from papertrader.strategies import Signal, Strategy
from tests.conftest import make_ohlc

IST = ZoneInfo("Asia/Kolkata")
TABLE_NAMES = ["decisions", "orders", "trades", "positions", "daily_snapshots", "backtest_results"]


class AlwaysBuy(Strategy):
    name, label, warmup = "always_buy", "always buy", 1

    def frame(self, df):
        return pd.DataFrame({"signal": Signal.BUY.value}, index=df.index)

    def explain(self, row):
        return "Test strategy says buy."


class Unselected(AlwaysBuy):
    name, label = "unselected", "unselected"


class FixedSelection(Selection):
    """Selection stub that picks the given strategies without backtesting."""

    def __init__(self, as_of, names):
        super().__init__(as_of, {"out_of_sample": (as_of, as_of)}, [], None, None)
        self._names = list(names)

    @property
    def selected(self):
        return self._names

    def summary(self):
        return f"selected {self._names}"

    def to_rows(self):
        return []


def select_only(names):
    return lambda strategies, prices, cfg, as_of: FixedSelection(as_of, names)


def price_loader(n_days):
    """Loader returning the first n_days bars of fixed synthetic data for every ticker."""
    base = {
        "AAA.NS": make_ohlc(np.linspace(100, 120, 40), start="2024-01-01"),
        "BBB.NS": make_ohlc(np.linspace(200, 210, 40), start="2024-01-01"),
        "^NSEI": make_ohlc(np.linspace(20000, 21000, 40), start="2024-01-01"),
    }

    def load(tickers, cfg, refresh=False, now=None):
        return {t: base[t].iloc[:n_days] for t in tickers if t in base}, []
    return load


def runner(cfg, store, n_days, strategies=None, selected=("always_buy",)):
    return DailyRunner(cfg, store, strategies or [AlwaysBuy(), Unselected()], price_loader(n_days),
                       select_only(selected), now=dt.datetime(2024, 6, 1, 18, 0, tzinfo=IST))


def counts(store):
    return {t: store.sqlite.count(t) for t in TABLE_NAMES}


def test_first_run_queues_buys_and_logs_every_decision(cfg):
    store = Store(SQLiteStore(cfg.storage.sqlite_path))
    s = runner(cfg, store, 10).run()
    assert s.status == "ok" and len(s.days_processed) == 1
    rows = store.sqlite.rows("decisions")
    assert len(rows) == 2 * 2  # 2 stocks x 2 strategies
    assert all(r["reason"] for r in rows)
    actions = {(r["ticker"], r["strategy"]): r["action"] for r in rows}
    assert actions[("AAA.NS", "always_buy")] == "BUY"
    assert actions[("AAA.NS", "unselected")] == "NOT_SELECTED"
    orders = store.sqlite.rows("orders")
    assert {o["status"] for o in orders} == {"PENDING"}
    assert store.sqlite.count("positions") == 0  # nothing fills until the next open


def test_running_twice_does_not_duplicate_anything(cfg):
    store = Store(SQLiteStore(cfg.storage.sqlite_path))
    runner(cfg, store, 10).run()
    before = counts(store)
    s2 = runner(cfg, store, 10).run()
    assert s2.status == "noop" and "Already processed" in s2.message
    assert counts(store) == before
    assert store.sqlite.count("runs") == 2  # each execution is still recorded


def test_next_day_fills_at_open_and_sets_stop(cfg):
    store = Store(SQLiteStore(cfg.storage.sqlite_path))
    runner(cfg, store, 10).run()
    s = runner(cfg, store, 11).run()
    assert s.days_processed == [dt.date(2024, 1, 15)]
    pos = {p["ticker"]: p for p in store.sqlite.rows("positions")}
    assert set(pos) == {"AAA.NS", "BBB.NS"}
    aaa_open = make_ohlc(np.linspace(100, 120, 40), start="2024-01-01")["Open"].iloc[10]
    assert pos["AAA.NS"]["entry_price"] == pytest.approx(aaa_open)
    assert pos["AAA.NS"]["stop_price"] == pytest.approx(aaa_open * 0.95)
    # 10% cap per position
    assert pos["AAA.NS"]["entry_price"] * pos["AAA.NS"]["qty"] <= 0.1 * cfg.capital


def test_missed_days_are_caught_up_in_order(cfg):
    store = Store(SQLiteStore(cfg.storage.sqlite_path))
    runner(cfg, store, 10).run()
    s = runner(cfg, store, 14).run()
    assert len(s.days_processed) == 4
    dates = [r["as_of_date"] for r in store.sqlite.rows("daily_snapshots", "as_of_date")]
    assert dates == sorted(set(dates)) and len(dates) == 5


def test_force_recomputes_without_duplicates(cfg):
    store = Store(SQLiteStore(cfg.storage.sqlite_path))
    runner(cfg, store, 10).run()
    runner(cfg, store, 11).run()
    before = counts(store)
    s = runner(cfg, store, 11).run(force=True)
    assert s.status == "ok" and s.days_processed == [dt.date(2024, 1, 15)]
    assert counts(store) == before


def test_no_selected_strategy_means_no_orders(cfg):
    store = Store(SQLiteStore(cfg.storage.sqlite_path))
    runner(cfg, store, 10, selected=()).run()
    assert store.sqlite.count("orders") == 0
    snap = store.sqlite.rows("daily_snapshots")[0]
    assert snap["equity"] == pytest.approx(cfg.capital)


def test_max_positions_blocks_with_reason(cfg, tmp_path):
    from dataclasses import replace as dc_replace
    cfg2 = dc_replace(cfg, risk=dc_replace(cfg.risk, max_open_positions=1))
    store = Store(SQLiteStore(tmp_path / "x.db"))
    runner(cfg2, store, 10).run()
    acts = {r["ticker"]: r for r in store.sqlite.rows("decisions") if r["strategy"] == "always_buy"}
    assert acts["AAA.NS"]["action"] == "BUY"
    assert acts["BBB.NS"]["action"] == "BLOCKED_BY_RISK" and "limit of 1" in acts["BBB.NS"]["reason"]


class FlakyRemote:
    def __init__(self, up=False):
        self.up, self.applied, self.state = up, [], None

    def apply(self, ops):
        if not self.up:
            raise ConnectionError("supabase down")
        self.applied.extend(ops)

    def latest_state(self, before=None):
        if not self.up:
            raise ConnectionError("supabase down")
        return self.state


def test_supabase_down_with_no_local_state_refuses_to_trade(cfg):
    store = Store(SQLiteStore(cfg.storage.sqlite_path), FlakyRemote(up=False))
    s = runner(cfg, store, 10).run()
    assert s.status == "aborted"
    assert store.sqlite.count("orders") == 0


def test_supabase_outage_is_replayed_from_outbox(cfg):
    remote = FlakyRemote(up=True)
    store = Store(SQLiteStore(cfg.storage.sqlite_path), remote)
    runner(cfg, store, 10).run()
    assert store.pending_sync() == 0 and remote.applied

    remote.up = False
    remote.applied.clear()
    s = runner(cfg, store, 11).run()  # local state exists -> runs on SQLite
    assert s.status == "ok" and store.pending_sync() > 0

    remote.up = True
    assert store.sync(force=True) is None
    assert store.pending_sync() == 0
    tables = {op.table for op in remote.applied}
    assert {"daily_snapshots", "orders", "positions", "runs"} <= tables


def test_supabase_schema_matches_python_schema():
    sql = (Path(__file__).resolve().parents[1] / "supabase" / "schema.sql").read_text()
    for table, (pk, cols) in TABLES.items():
        m = re.search(rf"create table if not exists public\.{table}\s*\((.*?)\n\);", sql, re.S | re.I)
        assert m, f"{table} missing from schema.sql"
        body = m.group(1)
        found = {line.strip().split()[0].strip('"') for line in body.splitlines()
                 if line.strip() and not line.strip().lower().startswith(("primary", "unique", "constraint", "--"))}
        assert found == set(cols), f"{table}: schema.sql {sorted(found ^ set(cols))} differ"
