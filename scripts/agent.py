"""The trading agent's desks from the command line. PAPER TRADING ONLY.

Each command is one desk. The Claude Code skills in .claude/skills/nse-* call
these, and each step reads what the last one wrote in data/agent/:

  scan -> news / events / insider / analyst -> entry -> plan -> timemachine / odds
       -> size -> risk -> ticket (show, approve with an exact "yes", paper fill) -> alerts

Examples:
  python scripts/agent.py clock
  python scripts/agent.py scan --universe nifty50
  python scripts/agent.py scan --orb --universe nifty50 --at 10:15
  python scripts/agent.py news INFY --hours 24
  python scripts/agent.py events RELIANCE
  python scripts/agent.py insider RELIANCE --days 30
  python scripts/agent.py analyst TCS
  python scripts/agent.py entry INFY
  python scripts/agent.py plan INFY --from-entry --stop or_mid --target-r 1
  python scripts/agent.py plan INFY --entry 1086 --stop pct --stop-pct 2 --target-r 2 --style swing
  python scripts/agent.py timemachine            # 10,000 replays of the planned trade + ORB history
  python scripts/agent.py odds INFY --strategy ma_crossover
  python scripts/agent.py size --max-loss 1000
  python scripts/agent.py risk
  python scripts/agent.py ticket show
  python scripts/agent.py ticket approve INFY-20261029-101500 --answer yes
  python scripts/agent.py ticket fill INFY-20261029-101500 --price 1086
  python scripts/agent.py ticket close INFY-20261029-101500 --price 1092 --reason target
  python scripts/agent.py alerts add-plan | check | list
  python scripts/agent.py brief INFY --strategy ma_crossover   # every desk, like the daily run
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from papertrader.agent import market  # noqa: E402
from papertrader.agent.types import symbol, yahoo  # noqa: E402

AGENT_DIR = ROOT / "data" / "agent"
PLAN = AGENT_DIR / "plan.json"
ENTRY = AGENT_DIR / "entry.json"


def _cfg():
    from papertrader.config import load_config
    return load_config()


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def _read(path: Path):
    if not path.exists():
        sys.exit(f"{path.relative_to(ROOT)} not found: run the previous step first.")
    return json.loads(path.read_text(encoding="utf-8"))


def _finding(f) -> None:
    print(f"[{f.desk}] {f.status.upper()}: {f.summary}")


def _at(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    if len(value) <= 5:
        h, m = value.split(":")
        return dt.datetime.combine(market.now_ist().date(), dt.time(int(h), int(m)), market.IST)
    return market.now_ist(dt.datetime.fromisoformat(value))


def _daily(tickers: list[str], days: int = 400):
    import dataclasses
    from papertrader.data import load_prices

    cfg = _cfg()
    data_cfg = dataclasses.replace(cfg.data, start=dt.date.today() - dt.timedelta(days=days),
                                   cache_dir=cfg.data.cache_dir / "scan")
    prices, warnings = load_prices(tickers, data_cfg)
    for w in warnings[:5]:
        print("warning:", w)
    return prices


def _universe(name: str) -> list[str]:
    if name == "config":
        return _cfg().universe
    from papertrader.agent.universe import load_universe
    return load_universe(name)


# ------------------------------------------------------------------ commands
def cmd_clock(a) -> None:
    print(market.market_status(_at(a.now)).describe())


def cmd_scan(a) -> None:
    from papertrader.agent.scanner import describe_hits, scan_daily, scan_orb

    tickers = [yahoo(t) for t in a.tickers] if a.tickers else _universe(a.universe)
    if a.orb:
        from papertrader.agent.intraday import load_intraday
        bars = {t: load_intraday(t, "5m", days=2) for t in tickers}
        at = _at(a.at)
        hits = scan_orb(bars, at=at.time() if at else None, or_minutes=a.or_minutes)
    else:
        hits = scan_daily(_daily(tickers))
    print(f"{len(hits)} setup(s) in {len(tickers)} stocks.")
    print(describe_hits(hits, limit=a.limit))


def cmd_news(a) -> None:
    from papertrader.agent.news import fetch_news, news_finding

    t = yahoo(a.ticker)
    try:
        items = fetch_news(t, hours=a.hours)
    except Exception as exc:
        sys.exit(f"News unavailable: {exc}")
    _finding(news_finding(t, items))
    for i in items[: a.limit]:
        tags = f" [{', '.join(i.tags)}]" if i.tags else ""
        print(f"- {i.published:%d %b %H:%M} {i.title} ({i.source}){tags}")


def cmd_events(a) -> None:
    from papertrader.agent.events import blackout_check, upcoming_events

    t = yahoo(a.ticker)
    _finding(blackout_check(t, on=market.now_ist().date(), days_before=a.days_before, days_after=a.days_after))
    try:
        for e in upcoming_events(t)[:10]:
            print(f"- {e.date:%a %d %b %Y}: {e.purpose} ({e.source})")
    except Exception as exc:
        print(f"Event list unavailable: {exc}")


def cmd_insider(a) -> None:
    from papertrader.agent.insider import bulk_block_deals, insider_check, insider_trades

    t = yahoo(a.ticker)
    _finding(insider_check(t, days=a.days))
    for label, fn in (("insider trade", insider_trades), ("bulk/block deal", bulk_block_deals)):
        try:
            for r in fn(t, days=a.days)[:15]:
                print(f"- {label}: {json.dumps(r, default=str)}")
        except Exception as exc:
            print(f"- {label}s unavailable: {exc}")


def cmd_analyst(a) -> None:
    from papertrader.agent.analyst import analyst_check, earnings_summary

    t = yahoo(a.ticker)
    _finding(analyst_check(t))
    try:
        for b in earnings_summary(t).get("bullets", []):
            print(f"- {b}")
    except Exception as exc:
        print(f"Quarterly numbers unavailable: {exc}")


def cmd_entry(a) -> None:
    from papertrader.agent.entry import check_orb
    from papertrader.agent.intraday import load_intraday

    t = yahoo(a.ticker)
    state = check_orb(load_intraday(t, "5m", days=5), at=_at(a.at), or_minutes=a.or_minutes)
    print(f"{symbol(t)}: {state.status.upper()} - {state.reason}")
    _write(ENTRY, {"ticker": t, **state.to_dict()})


def cmd_plan(a) -> None:
    from papertrader.agent.exitplan import PlanError, describe_plan, plan_trade

    t = yahoo(a.ticker)
    kw = {}
    entry = a.entry
    if a.from_entry:
        e = _read(ENTRY)
        if e["ticker"] != t:
            sys.exit(f"data/agent/entry.json is for {e['ticker']}, not {t}.")
        if e["status"] != "triggered" and entry is None:
            sys.exit(f"Entry is {e['status']}, not triggered: give --entry to plan anyway.")
        entry = entry or e["trigger_price"]
        kw = {"range_high": e.get("range_high"), "range_low": e.get("range_low")}
    if entry is None:
        sys.exit("Give --entry PRICE or --from-entry.")
    try:
        plan = plan_trade(t, float(entry), stop_method=a.stop, stop_pct=a.stop_pct, atr=a.atr,
                          atr_multiple=a.atr_multiple, stop_price=a.stop_price, target_r=a.target_r,
                          style=a.style, **kw)
    except PlanError as exc:
        sys.exit(f"No plan: {exc}")
    _write(PLAN, plan.to_dict())
    print(describe_plan(plan))
    print(f"Saved {PLAN.relative_to(ROOT)}.")


def cmd_timemachine(a) -> None:
    from papertrader.agent.exitplan import TradePlan
    from papertrader.agent.intraday import bars_on, load_intraday
    from papertrader.agent.timemachine import describe_session, replay_orb_history, simulate_session

    plan = TradePlan.from_dict(_read(PLAN))
    if plan.style != "intraday":
        sys.exit("The session replay is for intraday plans; for a swing trade use: agent.py odds TICKER --strategy NAME")
    bars = load_intraday(plan.ticker, "5m", days=59)
    if bars.empty:
        sys.exit("No 5-minute bars available.")
    today = market.now_ist().date()
    sessions = sorted({ts.date() for ts in bars.index})
    day = today if today in sessions else sessions[-1]
    hist, now_bars = bars[bars.index.date < day], bars_on(bars, day)
    if a.at:
        now_bars = now_bars[now_bars.index <= _at(a.at)]
    res = simulate_session(hist, now_bars, plan, n=a.n, seed=a.seed)
    print(describe_session(res))
    orb = replay_orb_history(hist)
    print(f"History: the opening-range breakout fired on {orb['fired']} of {orb['sessions']} past sessions: "
          f"{orb['target']} hit the target, {orb['stop']} the stop, {orb['neither']} neither "
          f"(average {orb['avg_r']:+.2f}R).")
    _write(AGENT_DIR / "timemachine.json", {"session": res, "orb_history": {k: v for k, v in orb.items() if k != "days"}})


def cmd_odds(a) -> None:
    from papertrader.agent.timemachine import describe_odds, signal_odds
    from papertrader.data import load_prices
    from papertrader.strategies import build_strategies

    cfg = _cfg()
    strategies = {s.name: s for s in build_strategies(cfg.strategies)}
    if a.strategy not in strategies:
        sys.exit(f"Unknown strategy {a.strategy!r}; choose from {sorted(strategies)}.")
    t = yahoo(a.ticker)
    prices, _ = load_prices([t], cfg.data)
    if t not in prices:
        sys.exit(f"No daily data for {t}.")
    res = signal_odds(prices[t], strategies[a.strategy], cfg.risk.exit_rules(), cfg.costs.model(),
                      lookback_days=a.lookback_days, ticker=t)
    print(describe_odds(res))


def cmd_size(a) -> None:
    from papertrader.agent.budget import bracket_ticket, describe_sizing, size_position
    from papertrader.agent.exitplan import TradePlan
    from papertrader.agent.tickets import load_tickets, save_tickets

    plan = TradePlan.from_dict(_read(PLAN))
    account = a.account or _cfg().capital
    sizing = size_position(plan, account=account, max_loss=a.max_loss, risk_pct=a.risk_pct,
                           max_position_pct=a.max_position_pct, cash=a.cash)
    print(describe_sizing(sizing))
    if not sizing.get("qty"):
        return
    ticket = bracket_ticket(plan, sizing)
    tickets = [t for t in load_tickets() if t.ticket_id != ticket.ticket_id] + [ticket]
    save_tickets(None, tickets)
    print(f"Draft ticket {ticket.ticket_id} saved. Next: agent.py risk")


def _limits(a):
    from papertrader.agent.riskcheck import RiskLimits
    return RiskLimits(max_loss_per_trade=a.max_loss, max_daily_loss=a.max_daily_loss,
                      max_trades_per_day=a.max_trades, min_reward_risk=a.min_rr)


def cmd_risk(a) -> None:
    from papertrader.agent.alerts import fetch_quotes
    from papertrader.agent.exitplan import TradePlan
    from papertrader.agent.riskcheck import check_ticket
    from papertrader.agent.tickets import ledger_today, load_ledger, load_tickets, save_tickets

    plan = TradePlan.from_dict(_read(PLAN))
    tickets = load_tickets()
    drafts = [t for t in tickets if t.status == "draft"]
    if not drafts:
        sys.exit("No draft tickets. Run agent.py size first.")
    today = ledger_today(load_ledger())
    account = a.account or _cfg().capital
    for t in drafts:
        q = fetch_quotes([t.ticker]).get(yahoo(t.ticker), {})
        v = check_ticket(t, plan, limits=_limits(a), account=account, cash=a.cash or account,
                         trades_today=today["trades_today"], realised_pnl_today=today["realised_pnl_today"],
                         prev_close=q.get("prev_close"))
        print(f"{t.ticket_id}: {v.describe()}")
    save_tickets(None, tickets)


def cmd_ticket(a) -> None:
    from papertrader.agent.tickets import (approve, close_paper, execute_paper, load_tickets, render,
                                           save_tickets)

    tickets = load_tickets()
    if a.action == "show":
        if not tickets:
            print("No tickets.")
        for t in tickets:
            print(render(t), "\n")
        return
    match = [t for t in tickets if t.ticket_id == a.ticket_id]
    if a.action == "close":
        print(close_paper(a.ticket_id, a.price, a.reason))
        return
    if not match:
        sys.exit(f"No ticket {a.ticket_id}.")
    t = match[0]
    if a.action == "approve":
        approve(t, a.answer or "")
        print(f"{t.ticket_id}: {t.status}. {' '.join(t.notes[-1:])}")
    elif a.action == "fill":
        if t.status != "approved":
            sys.exit(f"{t.ticket_id} is {t.status}: only an approved ticket can be filled (paper).")
        print(execute_paper(t, a.price))
    save_tickets(None, tickets)


def cmd_alerts(a) -> None:
    from papertrader.agent import alerts as al
    from papertrader.agent.exitplan import TradePlan

    rules = al.load_rules()
    if a.action == "add-plan":
        rules += al.rules_from_plan(TradePlan.from_dict(_read(PLAN)))
        al.save_rules(None, rules)
        print(f"{len(rules)} rule(s) saved.")
    elif a.action == "add":
        al.add_rule(rules, a.type, ticker=yahoo(a.ticker) if a.ticker else None, price=a.price, pct=a.pct,
                    at=a.at, note=a.note or "")
        al.save_rules(None, rules)
        print(f"{len(rules)} rule(s) saved.")
    elif a.action == "list":
        for r in rules:
            print(json.dumps(r, default=str))
    elif a.action == "check":
        state = al.load_state()
        tickers = sorted({r["ticker"] for r in rules if r.get("ticker")})
        fired = al.check(rules, al.fetch_quotes(tickers), None, state)
        al.save_state(None, state)
        print(al.format_fired(fired) if fired else "Nothing rang.")
        if fired and a.telegram:
            al.send(fired)


def cmd_brief(a) -> None:
    from papertrader.agent.orchestrator import Coordinator
    from papertrader.data import load_prices
    from papertrader.strategies import build_strategies

    cfg = _cfg()
    strategies = {s.name: s for s in build_strategies(cfg.strategies)}
    if a.strategy not in strategies:
        sys.exit(f"Unknown strategy {a.strategy!r}; choose from {sorted(strategies)}.")
    t = yahoo(a.ticker)
    prices, _ = load_prices([t], cfg.data)
    if t not in prices:
        sys.exit(f"No daily data for {t}.")
    desks = tuple(a.desks.split(",")) if a.desks else cfg.agent.desks
    import dataclasses
    coord = Coordinator(dataclasses.replace(cfg.agent, desks=desks), cfg.costs.model(), cfg.risk.exit_rules())
    day = prices[t].index[-1].date()
    b = coord.brief(day, t, strategies[a.strategy], prices[t])
    print(f"{symbol(t)} / {a.strategy} on {day}: {b.verdict}")
    for f in b.findings:
        _finding(f)


def main() -> int:
    p = argparse.ArgumentParser(description="PaperTrader agent desks (PAPER MONEY ONLY).")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("clock", help="NSE open / closed, next open")
    s.add_argument("--now", help="pin the clock, 'YYYY-MM-DD HH:MM' IST")
    s.set_defaults(fn=cmd_clock)

    s = sub.add_parser("scan", help="chart scan: daily setups or opening-range breakouts")
    s.add_argument("--universe", default="nifty50", choices=["config", "nifty50", "nifty100", "nifty500"])
    s.add_argument("--tickers", nargs="*")
    s.add_argument("--orb", action="store_true", help="intraday opening-range breakouts instead of daily setups")
    s.add_argument("--at", help="HH:MM IST (ORB: bars completed by then)")
    s.add_argument("--or-minutes", type=int, default=15)
    s.add_argument("--limit", type=int, default=25)
    s.set_defaults(fn=cmd_scan)

    for name, fn, extra in (("news", cmd_news, [("--hours", int, 24), ("--limit", int, 15)]),
                            ("events", cmd_events, [("--days-before", int, 2), ("--days-after", int, 1)]),
                            ("insider", cmd_insider, [("--days", int, 30)]),
                            ("analyst", cmd_analyst, [])):
        s = sub.add_parser(name)
        s.add_argument("ticker")
        for flag, typ, default in extra:
            s.add_argument(flag, type=typ, default=default)
        s.set_defaults(fn=fn)

    s = sub.add_parser("entry", help="opening-range breakout timer")
    s.add_argument("ticker")
    s.add_argument("--at", help="HH:MM IST")
    s.add_argument("--or-minutes", type=int, default=15)
    s.set_defaults(fn=cmd_entry)

    s = sub.add_parser("plan", help="stop and target before entry -> data/agent/plan.json")
    s.add_argument("ticker")
    s.add_argument("--entry", type=float)
    s.add_argument("--from-entry", action="store_true", help="use data/agent/entry.json (trigger + range)")
    s.add_argument("--stop", default="or_mid", choices=["pct", "atr", "or_mid", "or_low", "price"])
    s.add_argument("--stop-pct", type=float)
    s.add_argument("--stop-price", type=float)
    s.add_argument("--atr", type=float)
    s.add_argument("--atr-multiple", type=float, default=2.0)
    s.add_argument("--target-r", type=float, default=1.0)
    s.add_argument("--style", default="intraday", choices=["intraday", "swing"])
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("timemachine", help="10,000 replays of the planned intraday trade + ORB history")
    s.add_argument("--n", type=int, default=10000)
    s.add_argument("--seed", type=int, default=19)
    s.add_argument("--at", help="HH:MM IST: only today's bars up to then")
    s.set_defaults(fn=cmd_timemachine)

    s = sub.add_parser("odds", help="past odds of a strategy's signals on one stock (daily bars)")
    s.add_argument("ticker")
    s.add_argument("--strategy", required=True)
    s.add_argument("--lookback-days", type=int, default=1500)
    s.set_defaults(fn=cmd_odds)

    s = sub.add_parser("size", help="position size from the max loss -> draft ticket")
    s.add_argument("--account", type=float)
    s.add_argument("--cash", type=float)
    s.add_argument("--max-loss", type=float)
    s.add_argument("--risk-pct", type=float)
    s.add_argument("--max-position-pct", type=float, default=20)
    s.set_defaults(fn=cmd_size)

    s = sub.add_parser("risk", help="PASS / BLOCK every draft ticket")
    s.add_argument("--account", type=float)
    s.add_argument("--cash", type=float)
    s.add_argument("--max-loss", type=float, default=1000)
    s.add_argument("--max-daily-loss", type=float, default=3000)
    s.add_argument("--max-trades", type=int, default=3)
    s.add_argument("--min-rr", type=float, default=1.0)
    s.set_defaults(fn=cmd_risk)

    s = sub.add_parser("ticket", help="show / approve / fill / close paper tickets")
    s.add_argument("action", choices=["show", "approve", "fill", "close"])
    s.add_argument("ticket_id", nargs="?")
    s.add_argument("--answer", help='approval needs exactly "yes"')
    s.add_argument("--price", type=float)
    s.add_argument("--reason", default="manual")
    s.set_defaults(fn=cmd_ticket)

    s = sub.add_parser("alerts", help="price / stop / target / time alerts")
    s.add_argument("action", choices=["add-plan", "add", "list", "check"])
    s.add_argument("--type", default="above")
    s.add_argument("--ticker")
    s.add_argument("--price", type=float)
    s.add_argument("--pct", type=float)
    s.add_argument("--at")
    s.add_argument("--note")
    s.add_argument("--telegram", action="store_true", help="also send what rang to Telegram")
    s.set_defaults(fn=cmd_alerts)

    s = sub.add_parser("brief", help="ask every desk about a buy, like the daily run does")
    s.add_argument("ticker")
    s.add_argument("--strategy", required=True)
    s.add_argument("--desks", help="comma list, default from config.yaml agent.desks")
    s.set_defaults(fn=cmd_brief)

    a = p.parse_args()
    if a.cmd == "ticket" and a.action != "show" and not a.ticket_id:
        p.error("ticket id required")
    if a.cmd == "ticket" and a.action in ("fill", "close") and a.price is None:
        p.error("--price required")
    print("PAPER MONEY ONLY - nothing here places a real order.")
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
