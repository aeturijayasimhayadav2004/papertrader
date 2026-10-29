"""Coordinator + runner integration, with stub desks (no network)."""
import dataclasses
import datetime as dt

from papertrader.agent.orchestrator import CAUTION, GO, VERDICT_BLOCK, Coordinator, verdict_of
from papertrader.agent.types import BLOCK, INFO, UNKNOWN, WARN, Finding
from papertrader.config import AgentConfig
from papertrader.costs import flat
from papertrader.exits import ExitRules
from papertrader.notify import format_summary
from papertrader.storage import SQLiteStore, Store
from tests.test_runner import AlwaysBuy, runner


def desk(status, summary="stub", calls=None):
    def f(ticker, day, strategy, history):
        if calls is not None:
            calls.append((ticker, strategy.name))
        return Finding("stub", status, summary)
    return f


def boom(ticker, day, strategy, history):
    raise RuntimeError("source down")


def coordinator(desks, **kw):
    cfg = AgentConfig(enabled=True, desks=tuple(desks), **kw)
    return Coordinator(cfg, flat(0.002), ExitRules(), desks=desks)


def test_verdicts():
    f = lambda s: Finding("x", s, "s")  # noqa: E731
    assert verdict_of([f(INFO), f(INFO)], False) == GO
    assert verdict_of([f(INFO), f(WARN)], False) == CAUTION
    assert verdict_of([f(WARN), f(BLOCK)], False) == VERDICT_BLOCK
    assert verdict_of([f(UNKNOWN)], False) == GO
    assert verdict_of([f(UNKNOWN)], True) == VERDICT_BLOCK


def test_failing_desk_becomes_unknown_and_does_not_block_by_default():
    c = coordinator({"news": boom, "events": desk(INFO)})
    b = c.brief(dt.date(2024, 1, 2), "AAA.NS", AlwaysBuy(), None)
    assert b.verdict == GO and b.findings[0].status == UNKNOWN and "source down" in b.findings[0].summary
    c2 = coordinator({"news": boom}, block_on_unknown=True)
    assert c2.brief(dt.date(2024, 1, 2), "AAA.NS", AlwaysBuy(), None).blocked


def test_stock_level_desks_are_asked_once_per_stock_per_day():
    calls = []
    c = coordinator({"news": desk(INFO, calls=calls)})
    for _ in range(3):
        c.brief(dt.date(2024, 1, 2), "AAA.NS", AlwaysBuy(), None)
    assert len(calls) == 1


def test_runner_skips_buy_the_agent_blocks_and_saves_brief(cfg):
    store = Store(SQLiteStore(cfg.storage.sqlite_path))
    c = coordinator({"events": desk(BLOCK, "Results board meeting in 1 trading day.")})
    r = runner(cfg, store, 10)
    r.coordinator = c
    s = r.run()
    rows = {(d["ticker"], d["strategy"]): d for d in store.sqlite.rows("decisions")}
    aaa = rows[("AAA.NS", "always_buy")]
    assert aaa["action"] == "BLOCKED_BY_RISK" and "Blocked by the agent" in aaa["reason"]
    assert store.sqlite.count("orders") == 0
    briefs = store.sqlite.rows("agent_briefs")
    assert {b["verdict"] for b in briefs} == {"BLOCK"}
    # the unselected strategy's BUY gets an information-only brief too
    assert any("information only" in b["summary"] for b in briefs)
    assert "Agent briefs:" in format_summary(s)


def test_runner_buys_when_agent_has_no_objection(cfg):
    store = Store(SQLiteStore(cfg.storage.sqlite_path))
    r = runner(cfg, store, 10)
    r.coordinator = coordinator({"news": desk(WARN, "Broker cut its target.")})
    r.run()
    aaa = {(d["ticker"], d["strategy"]): d for d in store.sqlite.rows("decisions")}[("AAA.NS", "always_buy")]
    assert aaa["action"] == "BUY" and "with caution" in aaa["reason"]


def test_agent_only_runs_on_the_latest_day_when_catching_up(cfg):
    store = Store(SQLiteStore(cfg.storage.sqlite_path))
    runner(cfg, store, 10, selected=()).run()
    calls = []
    r = runner(cfg, store, 14, selected=())  # nothing bought, so the unselected BUY is briefed daily
    r.coordinator = coordinator({"news": desk(INFO, calls=calls)})
    s = r.run()
    assert len(s.days_processed) == 4
    days = {b["as_of_date"] for b in store.sqlite.rows("agent_briefs")}
    assert days == {str(s.days_processed[-1])}


def test_scan_rows_saved_and_scan_failure_is_harmless(cfg):
    cfg = dataclasses.replace(cfg, agent=AgentConfig(enabled=True, desks=(), scan_universe="config"))
    store = Store(SQLiteStore(cfg.storage.sqlite_path))
    hit = {"ticker": "AAA.NS", "setup": "breakout", "close": 120.0, "score": 2.0, "detail": "New 20-day high."}
    r = runner(cfg, store, 10)
    r.coordinator, r.scanner = coordinator({}), (lambda cfg, d, u, now=None: [hit])
    s = r.run()
    assert store.sqlite.rows("agent_scans")[0]["setup"] == "breakout" and s.scan

    def broken(cfg, d, u, now=None):
        raise RuntimeError("niftyindices down")
    r2 = runner(cfg, store, 11)
    r2.coordinator, r2.scanner = coordinator({}), broken
    assert r2.run().status == "ok"
