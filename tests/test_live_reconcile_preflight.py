"""Reconciliation diffs and the go-live preflight, with fakes for every network probe."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from papertrader.agent.market import IST
from papertrader.broker import Position
from papertrader.live.executor import preflight_problem
from papertrader.live.interface import CNC, MIS, BrokerPosition, Funds, Holding
from papertrader.live.reconcile import CRITICAL, INFO, WARNING, reconcile, reconcile_session
from papertrader.live.simulated import SimulatedExchange

NOW = dt.datetime(2026, 10, 29, 10, 0, tzinfo=IST)
ROOT = Path(__file__).resolve().parents[1]


def _load_preflight():
    spec = importlib.util.spec_from_file_location("preflight_script", ROOT / "scripts" / "preflight.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pf = _load_preflight()


# ----- reconciliation -----

def test_reconcile_clean_when_holdings_and_cnc_positions_match():
    rep = reconcile({"INFY.NS": 10, "TCS.NS": 3},
                    [Holding("INFY", 6, 1500.0, t1_qty=4)], [BrokerPosition("TCS", CNC, 3, 3000.0)], as_of="2026-10-29")
    assert rep.clean and rep.checked_symbols == 2 and "clean" in rep.summary()


def test_reconcile_severities():
    pos = Position("HDFCBANK.NS", 5, dt.date(2026, 10, 1), 1600.0, 0.0, 1500.0, "sma", 1610.0)
    rep = reconcile({"INFY.NS": 10, "TCS.NS": 2, "HDFCBANK.NS": pos, "ITC.NS": {"qty": 4, "entry_price": 400.0}},
                    [Holding("INFY", 7, 1500.0), Holding("RELIANCE", 1, 2500.0), Holding("HDFCBANK", 5, 1700.0),
                     Holding("ITC", 4, 401.0), Holding("GOLDBEES", 100, 60.0)],
                    [BrokerPosition("SBIN", MIS, 10, 800.0)],
                    bot_cash=10_000.0, funds=Funds(available=5_000.0), ignore_symbols=["GOLDBEES"])
    by = {(d.kind, d.symbol): d.severity for d in rep.differences}
    assert by[("qty_mismatch", "INFY")] == CRITICAL
    assert by[("missing_at_broker", "TCS")] == CRITICAL
    assert by[("extra_at_broker", "RELIANCE")] == WARNING
    assert by[("avg_price", "HDFCBANK")] == INFO
    assert ("avg_price", "ITC") not in by                     # within tolerance
    assert by[("ignored", "GOLDBEES")] == INFO
    assert by[("mis_open", "SBIN")] == WARNING
    assert by[("cash", "")] == WARNING
    assert not rep.clean and rep.worst == CRITICAL
    assert "INFY" in rep.summary()


def test_reconcile_cash_within_tolerance_is_info():
    rep = reconcile({}, [], bot_cash=10_000.0, funds=Funds(available=10_050.0))
    assert rep.clean and rep.differences[0].severity == INFO


def test_reconcile_session_against_simulated(tmp_path):
    ex = SimulatedExchange(cash=20_000, holdings={"INFY": (3, 1500.0)})
    ex.set_time(NOW)
    ex.login()
    rep = reconcile_session(ex, {"INFY.NS": 3}, bot_cash=20_000.0)
    assert rep.clean
    saved = json.loads(rep.save(tmp_path / "r.json").read_text())
    assert saved["clean"] is True


# ----- preflight -----

def _snapshots(n: int, start_equity=1_000_000.0, step=500.0):
    d0 = dt.date(2026, 1, 1)
    return [{"as_of_date": (d0 + dt.timedelta(days=i)).isoformat(), "equity": start_equity + i * step}
            for i in range(n)]


class FakeVerified:
    name, verified = "fake", True

    def __init__(self):
        self.ex = SimulatedExchange()
        self.ex.set_time(NOW)
        self.ex.login()

    def session_status(self):
        return self.ex.session_status()


def _good_env():
    return {"PAPERTRADER_STATIC_IP": "203.0.113.7", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}


def _run(cfg, tmp_path, **over):
    rec = tmp_path / "reconcile.json"
    if not rec.exists():
        rec.write_text(json.dumps({"as_of": "2026-10-29", "clean": True, "worst": "info"}))
    kw = dict(cfg=cfg, env=_good_env(), now=NOW, run_tests=lambda: (True, "500 passed"),
              fetch_ip=lambda: "203.0.113.7",
              fetch_time=lambda: (NOW.astimezone(dt.timezone.utc), NOW.astimezone(dt.timezone.utc)),
              session_factory=FakeVerified, snapshots=lambda c: _snapshots(70),
              reconcile_path=rec, out_path=tmp_path / "preflight.json")
    kw.update(over)
    return pf.run_preflight(**kw)


def _status(report):
    return {r["name"]: r["ok"] for r in report["results"]}


def test_preflight_all_pass_with_fakes_writes_fresh_report(cfg, tmp_path):
    report = _run(cfg, tmp_path)
    assert report["passed"], report["lines"]
    saved = json.loads((tmp_path / "preflight.json").read_text())
    assert saved["passed"] is True and saved["timestamp"]
    assert preflight_problem(tmp_path / "preflight.json", NOW) == ""
    assert all(line.startswith("[PASS]") for line in report["lines"])


def test_preflight_fails_today_with_simulated_exchange(cfg, tmp_path):
    report = _run(cfg, tmp_path, session_factory=pf.default_session)
    st = _status(report)
    assert st["broker session"] is True and st["broker adapter"] is False and not report["passed"]
    assert "did not pass" in preflight_problem(tmp_path / "preflight.json", NOW)


@pytest.mark.parametrize("override,item", [
    ({"run_tests": None}, "tests"),
    ({"run_tests": lambda: (False, "3 failed")}, "tests"),
    ({"fetch_ip": lambda: "198.51.100.1"}, "static IP"),
    ({"env": {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}}, "static IP"),
    ({"env": {"PAPERTRADER_STATIC_IP": "203.0.113.7"}}, "telegram alerts"),
    ({"env": {**_good_env(), "PAPERTRADER_LIVE_CAPITAL_CAP": "100000"}}, "capital cap"),
    ({"env": {**_good_env(), "PAPERTRADER_KILL_SWITCH": "1"}}, "kill switch off"),
    ({"fetch_time": lambda: (NOW, NOW + dt.timedelta(seconds=30))}, "clock sync"),
    ({"snapshots": lambda c: _snapshots(30)}, "paper history"),
    ({"snapshots": lambda c: _snapshots(70, step=-5_000.0)}, "paper history"),
])
def test_preflight_item_fails(cfg, tmp_path, override, item):
    report = _run(cfg, tmp_path, **override)
    assert _status(report)[item] is False and not report["passed"]


def test_preflight_network_errors_fail_not_crash(cfg, tmp_path):
    def boom():
        raise OSError("offline")
    report = _run(cfg, tmp_path, fetch_ip=boom, fetch_time=boom)
    st = _status(report)
    assert st["static IP"] is False and st["clock sync"] is False


def test_preflight_reconciliation_dirty_or_missing(cfg, tmp_path):
    rec = tmp_path / "reconcile.json"
    rec.write_text(json.dumps({"as_of": "2026-10-29", "clean": False, "worst": "critical"}))
    assert _status(_run(cfg, tmp_path))["reconciliation"] is False
    report = _run(cfg, tmp_path, reconcile_path=tmp_path / "missing.json")
    assert _status(report)["reconciliation"] is False


def test_preflight_bad_config_path_fails(tmp_path):
    report = _run(None, tmp_path, cfg_path=tmp_path / "nope.yaml")
    assert _status(report)["config valid"] is False and not report["passed"]
