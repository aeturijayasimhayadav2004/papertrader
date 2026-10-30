from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from papertrader.agent.intraday_runner import (CycleReport, IntradayConfig, IntradaySession, completed_bars,
                                               resolve_exit)
from papertrader.agent.market import IST
from papertrader.agent.tickets import LiveTradingRefused, load_ledger
from papertrader.agent.types import BLOCK, OK, Finding

DAY = dt.date(2026, 10, 28)          # a Wednesday, NSE open
TICKER = "AAA.NS"


def at(h: int, m: int, day: dt.date = DAY) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(h, m), IST)


def history(n_days: int = 30, seed: int = 1, price: float = 1000.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = [d.date() for d in pd.bdate_range(end=DAY - dt.timedelta(days=1), periods=n_days)]
    idx = pd.DatetimeIndex([pd.Timestamp(dt.datetime.combine(d, dt.time(9, 15)), tz=IST)
                            + pd.Timedelta(minutes=5 * j) for d in days for j in range(75)])
    close = price * np.exp(np.cumsum(rng.normal(0, 0.0005, len(idx))))
    close = close / close[-1] * price                                  # end the history at `price`
    open_ = np.r_[close[0], close[:-1]]
    hi = np.maximum(open_, close) * 1.0005
    lo = np.minimum(open_, close) * 0.9995
    return pd.DataFrame({"Open": open_, "High": hi, "Low": lo, "Close": close, "Volume": 1e4}, index=idx)


def today(scenario: str = "flat") -> pd.DataFrame:
    """Range 998-1002 (09:15-09:25), the 09:35 bar closes 1004 -> trigger at 09:40, entry 1004,
    stop (range mid) 1000, target 1.5R = 1010."""
    rows = [(1000, 1002, 999, 1001), (1001, 1001.5, 998, 999), (999, 1001, 998.5, 1000),
            (1000, 1001.5, 999.5, 1001), (1001, 1004.5, 1000.5, 1004)]                # 09:15 .. 09:35
    n_after = 75 - len(rows)
    after = [(1004, 1006, 1003, 1005)] * n_after                                      # 09:40 ..
    if scenario == "target":
        after[2] = (1005, 1011, 1004, 1009)                                           # 09:50 bar
    elif scenario == "stop":
        after[2] = (1004, 1005, 999, 1000)
    elif scenario == "both":
        after[1] = (1004, 1011, 999, 1003)                                            # 09:45 bar
    elif scenario == "gap":
        after[1] = (997, 999, 996, 998)
    idx = pd.DatetimeIndex([pd.Timestamp(dt.datetime.combine(DAY, dt.time(9, 15)), tz=IST)
                            + pd.Timedelta(minutes=5 * j) for j in range(75)])
    data = rows + after
    return pd.DataFrame({"Open": [r[0] for r in data], "High": [r[1] for r in data],
                         "Low": [r[2] for r in data], "Close": [r[3] for r in data], "Volume": 1e4}, index=idx)


class Calls:
    def __init__(self) -> None:
        self.news = 0
        self.events = 0
        self.messages: list[str] = []


def session(tmp_path, scenario="flat", *, news=OK, events=OK, calls=None, **cfg_kw) -> IntradaySession:
    calls = calls if calls is not None else Calls()
    bars = pd.concat([history(), today(scenario)])
    kw = {"tickers": [TICKER], "min_p_target_minus_p_stop": None, "notify": True}
    kw.update(cfg_kw)
    cfg = IntradayConfig.from_dict(kw)

    def news_fn(t, now):
        calls.news += 1
        return Finding("news", news, f"{t}: news {news}")

    def events_fn(t, day):
        calls.events += 1
        return Finding("events", events, f"{t}: events {events}")

    s = IntradaySession(cfg, now=at(9, 40), bars_loader=lambda t, days, now: bars, news_fn=news_fn,
                        events_fn=events_fn, notifier=calls.messages.append, state_dir=tmp_path,
                        kill_switch=lambda: False)
    s.calls = calls
    return s


def run(s: IntradaySession, *times) -> list[CycleReport]:
    out = []
    for t in times:
        s.set_now(t)
        out.append(s.cycle())
    return out


def events_of(s, kind):
    return [r for r in s.journal_rows(DAY) if r["event"] == kind]


def test_entry_once_across_repeated_cycles(tmp_path):
    s = session(tmp_path)
    reports = run(s, at(9, 40), at(9, 40), at(9, 45), at(9, 50))
    ledger = load_ledger(tmp_path / "intraday_ledger.json")
    assert len(ledger) == 1
    rec = ledger[0]
    assert rec["status"] == "open" and rec["entry_price"] == 1004.0
    assert rec["stop_loss"] == 1000.0 and rec["target"] == 1010.0
    assert rec["qty"] > 0 and rec["qty"] * 4 <= 1000                      # max loss Rs 1000 incl. charges
    assert len(events_of(s, "entry")) == 1
    assert reports[0].rows[0]["event"] == "entry"
    assert all(r.rows == [] for r in reports[1:])
    assert s.calls.news == 1 and s.calls.events == 1                      # gates cached for the day
    assert sum("BUY" in m for m in s.calls.messages) == 1
    state = json.loads((tmp_path / f"intraday_state_{DAY}.json").read_text())
    assert state["decided"][TICKER] == "entry" and state["cycles"] == 4


def test_restart_new_process_does_not_trade_again(tmp_path):
    run(session(tmp_path), at(9, 40))
    s2 = session(tmp_path)
    run(s2, at(9, 45), at(9, 50))
    assert len(load_ledger(tmp_path / "intraday_ledger.json")) == 1
    assert len(events_of(s2, "entry")) == 1


def test_no_future_bars_before_trigger(tmp_path):
    s = session(tmp_path)
    (r,) = run(s, at(9, 38))                                              # the 09:35 bar is still forming
    assert r.triggered == 0 and r.rows == []


def test_target_exit(tmp_path):
    s = session(tmp_path, "target")
    run(s, at(9, 40), at(9, 50))
    assert load_ledger(tmp_path / "intraday_ledger.json")[0]["status"] == "open"   # 09:50 bar not complete
    run(s, at(9, 55))
    rec = load_ledger(tmp_path / "intraday_ledger.json")[0]
    assert rec["status"] == "closed" and rec["exit_reason"] == "target" and rec["exit_price"] == 1010.0
    assert rec["net_pnl"] > 0 and rec["exit_time"].startswith(f"{DAY}T09:55")
    (row,) = events_of(s, "exit")
    assert row["pnl"] == rec["net_pnl"] and row["details"]["exit_reason"] == "target"


def test_stop_exit(tmp_path):
    s = session(tmp_path, "stop")
    run(s, at(9, 40), at(10, 0), at(10, 5))
    rec = load_ledger(tmp_path / "intraday_ledger.json")[0]
    assert rec["exit_reason"] == "stop" and rec["exit_price"] == 1000.0 and rec["net_pnl"] < 0
    assert len(events_of(s, "exit")) == 1


def test_bar_touching_both_counts_as_stop(tmp_path):
    s = session(tmp_path, "both")
    run(s, at(9, 40), at(9, 50))
    rec = load_ledger(tmp_path / "intraday_ledger.json")[0]
    assert rec["exit_reason"] == "stop" and rec["exit_price"] == 1000.0


def test_gap_through_stop_fills_at_open(tmp_path):
    s = session(tmp_path, "gap")
    run(s, at(9, 40), at(9, 50))
    rec = load_ledger(tmp_path / "intraday_ledger.json")[0]
    assert rec["exit_reason"] == "stop" and rec["exit_price"] == 997.0


def test_flatten_at_flat_by_and_one_summary(tmp_path):
    s = session(tmp_path, "flat")
    run(s, at(9, 40), at(12, 0), at(15, 10))
    assert load_ledger(tmp_path / "intraday_ledger.json")[0]["status"] == "open"
    run(s, at(15, 15))
    rec = load_ledger(tmp_path / "intraday_ledger.json")[0]
    assert rec["exit_reason"] == "flat_by" and rec["exit_price"] == 1005.0
    assert rec["exit_time"].startswith(f"{DAY}T15:15")
    assert len(events_of(s, "flatten")) == 1
    run(s, at(15, 35), at(15, 40))
    summaries = [m for m in s.calls.messages if "PAPER MONEY" in m and "Trades:" in m]
    assert len(summaries) == 1 and "flat_by" in summaries[0]


def test_flatten_late_uses_the_close_by_flat_by(tmp_path):
    s = session(tmp_path, "flat")
    run(s, at(9, 40), at(15, 30))                                         # timer missed 15:15
    rec = load_ledger(tmp_path / "intraday_ledger.json")[0]
    assert rec["exit_reason"] == "flat_by" and rec["exit_time"].startswith(f"{DAY}T15:15")


def test_daily_loss_guard(tmp_path):
    lost = [{"ticket_id": "OLD-1", "ticker": "BBB.NS", "product": "MIS", "mode": "paper", "qty": 10,
             "entry_price": 500.0, "entry_time": f"{DAY}T09:30:00+05:30", "stop_loss": 490.0, "target": 520.0,
             "entry_charges": 1.0, "status": "closed", "exit_price": 200.0,
             "exit_time": f"{DAY}T09:35:00+05:30", "exit_reason": "stop", "exit_charges": 1.0,
             "gross_pnl": -3000.0, "net_pnl": -3002.0, "r": -30.0}]
    (tmp_path / "intraday_ledger.json").write_text(json.dumps(lost))
    s = session(tmp_path)
    run(s, at(9, 40), at(9, 45))
    ledger = load_ledger(tmp_path / "intraday_ledger.json")
    assert len(ledger) == 1                                               # no new entry
    blocks = events_of(s, "block")
    assert len(blocks) == 1 and "daily loss guard" in blocks[0]["reason"]


def test_holiday_is_a_noop(tmp_path):
    s = session(tmp_path)
    (r,) = run(s, dt.datetime(2026, 10, 20, 10, 0, tzinfo=IST))           # Dussehra
    assert r.status == "noop" and r.phase == "closed"
    assert list(tmp_path.iterdir()) == []


def test_weekend_and_preopen_are_noops(tmp_path):
    s = session(tmp_path)
    r1, r2 = run(s, dt.datetime(2026, 10, 31, 10, 0, tzinfo=IST), at(9, 5))
    assert r1.status == r2.status == "noop"
    assert list(tmp_path.iterdir()) == []


def test_news_block_respected(tmp_path):
    s = session(tmp_path, news=BLOCK)
    run(s, at(9, 40), at(9, 45))
    assert load_ledger(tmp_path / "intraday_ledger.json") == []
    (row,) = events_of(s, "block")
    assert row["reason"].startswith("news gate block") and s.calls.news == 1


def test_events_block_respected(tmp_path):
    s = session(tmp_path, events=BLOCK)
    run(s, at(9, 40))
    assert load_ledger(tmp_path / "intraday_ledger.json") == []
    (row,) = events_of(s, "block")
    assert row["reason"].startswith("events gate block")
    assert s.calls.news == 0                                              # events is asked first


def test_time_machine_gate_can_skip(tmp_path):
    s = session(tmp_path, min_p_target_minus_p_stop=1.01)                 # impossible edge
    run(s, at(9, 40))
    assert load_ledger(tmp_path / "intraday_ledger.json") == []
    (row,) = events_of(s, "skip")
    assert row["reason"].startswith("time machine") and row["details"]["tm"]["ok"] is True


def test_stale_trigger_skipped(tmp_path):
    s = session(tmp_path)
    run(s, at(11, 0))
    assert load_ledger(tmp_path / "intraday_ledger.json") == []
    (row,) = events_of(s, "skip")
    assert "min old" in row["reason"]


def test_outside_entry_window_no_entry(tmp_path):
    s = session(tmp_path, entry_start="09:45")
    (r,) = run(s, at(9, 40))
    assert r.rows == [] and any("entry window" in n for n in r.notes)


def test_live_mode_refused(tmp_path):
    with pytest.raises(LiveTradingRefused):
        IntradayConfig(mode="live")
    with pytest.raises(LiveTradingRefused):
        IntradayConfig.from_dict({"mode": "live", "auto_approve_paper": True})


def test_data_errors_never_raise(tmp_path):
    def broken(t, days, now):
        raise ConnectionError("yahoo down")
    s = IntradaySession(IntradayConfig(tickers=[TICKER]), now=at(10, 0), bars_loader=broken,
                        news_fn=lambda t, n: Finding("news", OK, "ok"),
                        events_fn=lambda t, d: Finding("events", OK, "ok"), notifier=None,
                        state_dir=tmp_path, kill_switch=lambda: False)
    r = s.cycle()
    assert r.status == "ok" and r.scanned == 0


def test_journal_row_shape(tmp_path):
    s = session(tmp_path, "target")
    run(s, at(9, 40), at(9, 55))
    rows = s.journal_rows()
    assert [r["event"] for r in rows] == ["entry", "exit"]
    keys = {"id", "ts", "day", "ticker", "event", "reason", "price", "qty", "ticket_id", "pnl", "details"}
    for r in rows:
        assert set(r) == keys
        assert r["day"] == DAY.isoformat() and r["ts"].endswith("+05:30")
        assert isinstance(r["details"], dict) and len(r["id"]) == 16
        json.dumps(r)
    assert rows[0]["ticket_id"] == rows[1]["ticket_id"]
    assert rows[0]["details"]["plan"]["stop"] == 1000.0
    assert len({r["id"] for r in rows}) == 2


def test_dry_run_writes_nothing(tmp_path):
    bars = pd.concat([history(), today()])
    s = IntradaySession(IntradayConfig(tickers=[TICKER], min_p_target_minus_p_stop=None), now=at(9, 40),
                        bars_loader=lambda t, d, n: bars, news_fn=lambda t, n: Finding("news", OK, "ok"),
                        events_fn=lambda t, d: Finding("events", OK, "ok"), state_dir=tmp_path, dry_run=True,
                        kill_switch=lambda: False)
    r = s.cycle()
    assert [x["event"] for x in r.rows] == ["entry"]
    assert list(tmp_path.iterdir()) == []


def test_resolve_exit_and_completed_bars():
    bars = today("target")
    assert completed_bars(bars, at(9, 40)).index[-1] == pd.Timestamp(at(9, 35))
    rec = {"entry_time": at(9, 40).isoformat(), "stop_loss": 1000.0, "target": 1010.0}
    assert resolve_exit(rec, bars, at(9, 50), dt.time(15, 15)) is None
    price, reason, when = resolve_exit(rec, bars, at(9, 55), dt.time(15, 15))
    assert (price, reason, when) == (1010.0, "target", at(9, 55))


def test_config_from_dict_parses_and_validates():
    cfg = IntradayConfig.from_dict({"entry_start": "09:30", "flat_by": "15:10", "watchlist": "Nifty100",
                                    "tickers": ["infy"]})
    assert cfg.entry_start == dt.time(9, 30) and cfg.flat_by == dt.time(15, 10)
    assert cfg.watchlist == "nifty100" and cfg.tickers == ["INFY.NS"]
    assert IntradayConfig.from_dict(None).watchlist == "nifty50"
    with pytest.raises(ValueError):
        IntradayConfig.from_dict({"watchlist": "sensex"})
    with pytest.raises(ValueError):
        IntradayConfig.from_dict({"entry_end": "15:20"})


def test_journal_rows_fit_agent_intraday_table(tmp_path):
    from papertrader.storage.schema import normalize_row
    s = session(tmp_path, "target")
    run(s, at(9, 40), at(9, 55))
    for r in s.journal_rows(DAY):
        normalize_row("agent_intraday", r)          # raises on unknown columns


def test_sync_upserts_and_never_raises(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    import papertrader.config as config_mod
    import papertrader.storage as storage_mod

    spec = importlib.util.spec_from_file_location(
        "intraday_loop", Path(__file__).resolve().parents[1] / "scripts" / "intraday_loop.py")
    loop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loop)

    s = session(tmp_path, "target")
    run(s, at(9, 40), at(9, 55))
    written = []

    class FakeStore:
        def write(self, ops):
            written.extend(ops)

    monkeypatch.setattr(config_mod, "load_config", lambda path=None: object())
    monkeypatch.setattr(storage_mod.Store, "from_config", classmethod(lambda cls, cfg: FakeStore()))
    assert loop.sync_journal(s, DAY) == 2
    assert written[0].table == "agent_intraday" and len(written[0].rows) == 2

    class Broken:
        def write(self, ops):
            raise ConnectionError("supabase down")

    monkeypatch.setattr(storage_mod.Store, "from_config", classmethod(lambda cls, cfg: Broken()))
    assert loop.sync_journal(s, DAY) == -1


def test_config_from_dict_rejects_unknown_keys_and_non_positive_limits():
    with pytest.raises(ValueError, match="unknown key"):
        IntradayConfig.from_dict({"max_daily_los": 3000})                 # a typo must not fall back silently
    for key in ("account", "max_loss_per_trade", "max_daily_loss", "max_trades_per_day"):
        for bad in (0, -1, "abc", None):
            with pytest.raises(ValueError, match=key):
                IntradayConfig.from_dict({key: bad})
    assert IntradayConfig.from_dict({"account": 500000, "max_trades_per_day": 2}).account == 500000


def test_project_config_yaml_intraday_section_parses():
    import yaml
    from pathlib import Path
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
    cfg = IntradayConfig.from_dict(raw.get("intraday") or {})
    assert cfg.mode == "paper" and cfg.entry_end == dt.time(14, 30) and cfg.max_daily_loss > 0


def test_kill_switch_env_any_case_and_configured_file(tmp_path, monkeypatch):
    from papertrader.agent import intraday_runner as ir
    import papertrader.config as config_mod

    switch = tmp_path / "STOP_NOW"

    class Trading:
        kill_switch_file = switch

    class Cfg:
        trading = Trading()

    monkeypatch.setattr(config_mod, "load_config", lambda path=None: Cfg())
    monkeypatch.delenv("PAPERTRADER_KILL_SWITCH", raising=False)
    assert ir.kill_switch_file() == switch
    assert ir._kill_switch_on() is False
    for v in ("1", "TRUE", " Yes ", "on", "ON"):
        monkeypatch.setenv("PAPERTRADER_KILL_SWITCH", v)
        assert ir._kill_switch_on() is True
    for v in ("0", "false", "", "off"):
        monkeypatch.setenv("PAPERTRADER_KILL_SWITCH", v)
        assert ir._kill_switch_on() is False
    switch.write_text("stop")
    assert ir._kill_switch_on() is True                                   # trading.kill_switch_file honoured

    def broken(path=None):
        raise FileNotFoundError("no config")
    monkeypatch.setattr(config_mod, "load_config", broken)
    assert ir.kill_switch_file() == ir.PROJECT_ROOT / "KILL_SWITCH"       # falls back


def test_entry_time_is_fill_bar_end_so_a_stop_in_the_next_bar_is_seen(tmp_path):
    """Regression: a stale download at the 09:45 cycle still ends with the 09:35 bar. The fill is that
    bar's close, so the entry is stamped 09:40 (its end), not 09:45 (the wall clock). The 09:40 bar,
    which touched the stop, must then be checked by the next cycle."""
    full = pd.concat([history(), today("flat")])
    i940 = full.index.get_loc(pd.Timestamp(at(9, 40)))
    full.iloc[i940, full.columns.get_indexer(["Open", "High", "Low", "Close"])] = [1004, 1005, 999, 1001]
    stale = full[full.index < pd.Timestamp(at(9, 40))]

    def loader(t, days, now):
        return stale if now < at(9, 50) else full

    s = IntradaySession(IntradayConfig(tickers=[TICKER], min_p_target_minus_p_stop=None), now=at(9, 45),
                        bars_loader=loader, news_fn=lambda t, n: Finding("news", OK, "ok"),
                        events_fn=lambda t, d: Finding("events", OK, "ok"), notifier=None,
                        state_dir=tmp_path, kill_switch=lambda: False)
    run(s, at(9, 45))
    rec = load_ledger(tmp_path / "intraday_ledger.json")[0]
    assert rec["entry_price"] == 1004.0 and rec["entry_time"].startswith(f"{DAY}T09:40")
    run(s, at(9, 50))
    rec = load_ledger(tmp_path / "intraday_ledger.json")[0]
    assert rec["status"] == "closed" and rec["exit_reason"] == "stop" and rec["exit_price"] == 1000.0
    assert rec["exit_time"].startswith(f"{DAY}T09:45")


def test_resolve_exit_reasons_match_exits_rule():
    rec = {"entry_time": at(9, 40).isoformat(), "stop_loss": 1000.0, "target": 1010.0}
    assert resolve_exit(rec, today("both"), at(9, 50), dt.time(15, 15))[:2] == (1000.0, "stop")
    assert resolve_exit(rec, today("gap"), at(9, 50), dt.time(15, 15))[:2] == (997.0, "stop")
    assert resolve_exit(rec, today("target"), at(9, 55), dt.time(15, 15))[:2] == (1010.0, "target")
