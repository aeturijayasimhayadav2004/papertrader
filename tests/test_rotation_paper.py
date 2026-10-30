"""Rotation paper account: month-end decisions, next-open fills, catch-up, restart safety."""
import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np

from papertrader.costs import SELL, flat
from papertrader.portfolio.paper import TABLE, Account, RotationPaper, fill_pending, is_rebalance_day
from papertrader.storage import SQLiteStore, Store
from tests.conftest import make_ohlc

IST = ZoneInfo("Asia/Kolkata")


def universe_prices(n=420):
    rng = np.random.default_rng(3)
    out = {}
    for i in range(14):
        drift = 0.0002 + 0.0003 * i  # higher index = stronger momentum
        closes = 100 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
        out[f"S{i:02d}.NS"] = make_ohlc(closes, start="2024-01-01")
    out["^NSEI"] = make_ohlc(np.linspace(20000, 24000, n), start="2024-01-01")
    return out


def loader(n_days):
    base = universe_prices()

    def load(tickers, cfg, refresh=False, now=None):
        return {t: base[t].iloc[:n_days] for t in tickers if t in base}, []
    return load


def paper(cfg, store, n_days):
    cfg = dataclasses.replace(cfg, sections={"portfolio": {"enabled": True, "universe": "custom",
                                                           "strategy": "momentum_12_1"}})
    return RotationPaper(cfg, store, price_loader=loader(n_days),
                         universe_loader=lambda name: [f"S{i:02d}.NS" for i in range(14)],
                         now=dt.datetime(2026, 1, 1, 18, tzinfo=IST))


def test_rebalance_day_is_last_trading_day_of_month():
    assert is_rebalance_day(dt.date(2026, 10, 30))       # Friday, next trading day is 2 Nov
    assert not is_rebalance_day(dt.date(2026, 10, 29))
    assert is_rebalance_day(dt.date(2026, 12, 31))


def test_fill_pending_sells_first_and_caps_buys_at_cash():
    acct = Account(cash=1000.0, holdings={"A": 10}, pending=[["A", SELL, 10], ["B", "BUY", 1000]])
    fills = fill_pending(acct, {"A": 100.0, "B": 50.0}, flat(0.0), dt.date(2026, 1, 2))
    assert acct.holdings == {"B": 40} and acct.cash == 0.0
    assert [f["side"] for f in fills] == [SELL, "BUY"]


def test_first_run_starts_in_cash_and_catch_up_rebalances_at_month_end(cfg):
    store = Store(SQLiteStore(cfg.storage.sqlite_path))
    paper(cfg, store, 300).run()
    rows = store.sqlite.rows(TABLE, order_by="as_of_date")
    assert len(rows) == 1 and rows[0]["equity"] == 1_000_000
    events = paper(cfg, store, 340).run()
    rows = store.sqlite.rows(TABLE, order_by="as_of_date")
    assert len(rows) == 41                                # every caught-up day saved once
    assert any("rebalance" in e for e in events)          # a month end fell inside the window
    held = [r for r in rows if r["n_holdings"]]
    assert held and held[0]["n_holdings"] == 10           # top 10 bought at the next open
    assert all(r["equity"] > 0 for r in rows)


def test_rerun_is_a_noop(cfg):
    store = Store(SQLiteStore(cfg.storage.sqlite_path))
    paper(cfg, store, 340).run()
    before = store.sqlite.count(TABLE)
    assert paper(cfg, store, 340).run() == [] and store.sqlite.count(TABLE) == before
