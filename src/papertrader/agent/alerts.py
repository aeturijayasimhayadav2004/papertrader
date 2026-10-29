"""Price, stop, target and clock alerts. Information only: an alert never places an order.

Rules live in data/agent/alerts.json, what already rang in data/agent/alerts_state.json.

Rule types (each rule is a dict with an "id" and a "type"):
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  ("direction": "up", "down" or "any"; default any)
  time            the IST clock passed "at" (HH:MM) on a trading day, e.g. a flat-by reminder
  opening_bell    NSE opened today (09:15 IST on a trading day)

A rule with "date" (YYYY-MM-DD) is only watched on that IST date; rules made from a
plan are for the plan's day. Price rules ring ONCE and then stay quiet; pct_move,
time and opening_bell are watched on trading days only and ring at most once a day.

Quotes: Yahoo's chart JSON (unofficial, free, delayed; for alerts only, never for
timing an order): https://query1.finance.yahoo.com/v8/finance/chart/INFY.NS?interval=1m&range=1d
meta.regularMarketPrice and meta.previousClose (or chartPreviousClose).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from papertrader.agent import market
from papertrader.agent.exitplan import TradePlan
from papertrader.agent.http import Fetch, get_json
from papertrader.agent.types import symbol, yahoo
from papertrader.config import PROJECT_ROOT

log = logging.getLogger(__name__)

AGENT_DIR = PROJECT_ROOT / "data" / "agent"
DEFAULT_RULES = AGENT_DIR / "alerts.json"
DEFAULT_STATE = AGENT_DIR / "alerts_state.json"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{t}?interval=1m&range=1d"
PRICE_TYPES = ("above", "below", "stop", "target")
TYPES = PRICE_TYPES + ("pct_move", "time", "opening_bell")
DAILY_TYPES = ("pct_move", "time", "opening_bell")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

Rule = dict[str, Any]


# ---------------------------------------------------------------- rules and state on disk

def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _write(path: Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def load_rules(path: str | Path | None = None) -> list[Rule]:
    return list(_read(Path(path or DEFAULT_RULES), {"rules": []}).get("rules", []))


def save_rules(path: str | Path | None, rules: list[Rule]) -> None:
    _write(Path(path or DEFAULT_RULES), {"rules": rules})


def load_state(path: str | Path | None = None) -> dict[str, Any]:
    return _read(Path(path or DEFAULT_STATE), {"fired": {}})


def save_state(path: str | Path | None, state: dict[str, Any]) -> None:
    _write(Path(path or DEFAULT_STATE), state)


# ---------------------------------------------------------------- making rules

def add_rule(rules: list[Rule], type: str, *, ticker: str | None = None, price: float | None = None,
             pct: float | None = None, direction: str = "any", at: str | dt.time | None = None,
             note: str = "", date: str | dt.date | None = None, rule_id: str | None = None) -> Rule:
    """Validate a rule, append it to `rules` (replacing one with the same id) and return it."""
    if type not in TYPES:
        raise ValueError(f"unknown alert type {type!r}; use one of {', '.join(TYPES)}")
    rule: Rule = {"type": type}
    if type in PRICE_TYPES + ("pct_move",):
        if not ticker:
            raise ValueError(f"a {type} alert needs a ticker")
    if ticker:
        rule["ticker"] = yahoo(ticker)
    if type in PRICE_TYPES:
        if price is None or price <= 0:
            raise ValueError(f"a {type} alert needs a positive price")
        rule["price"] = round(float(price), 2)
    if type == "pct_move":
        if not pct or pct <= 0:
            raise ValueError("a pct_move alert needs a positive pct")
        if direction not in ("up", "down", "any"):
            raise ValueError("direction must be up, down or any")
        rule.update(pct=float(pct), direction=direction)
    if type == "time":
        at = at.strftime("%H:%M") if isinstance(at, dt.time) else at
        if not at or not HHMM.match(str(at)):
            raise ValueError("a time alert needs at='HH:MM' (IST)")
        rule["at"] = str(at)
    if date:
        rule["date"] = date.isoformat() if isinstance(date, dt.date) else str(date)
    if note:
        rule["note"] = note
    sym = symbol(ticker).lower() if ticker else "nse"
    rule["id"] = rule_id or f"{sym}-{type}" + (f"-{rule.get('price', rule.get('pct', rule.get('at', '')))}"
                                               if type != "opening_bell" else "")
    rules[:] = [r for r in rules if r.get("id") != rule["id"]] + [rule]
    return rule


def rules_from_plan(plan: TradePlan, date: dt.date | None = None) -> list[Rule]:
    """Stop, target and flat-by alerts for one plan (for `date`, default today IST)."""
    day = (date or market.now_ist().date()).isoformat()
    sym = symbol(plan.ticker).lower()
    out: list[Rule] = []
    add_rule(out, "stop", ticker=plan.ticker, price=plan.stop, date=day, rule_id=f"{sym}-plan-stop",
             note=f"plan stop ({plan.stop_rule or 'stop'}): get out")
    add_rule(out, "target", ticker=plan.ticker, price=plan.target, date=day, rule_id=f"{sym}-plan-target",
             note=f"plan target {plan.r_multiple:g}R: take the profit")
    if plan.flat_by:
        add_rule(out, "time", ticker=plan.ticker, at=plan.flat_by, date=day, rule_id=f"{sym}-flat-by",
                 note="flat-by time: close the intraday position")
    return out


# ---------------------------------------------------------------- checking

def _rs(x: float) -> str:
    return f"Rs {x:,.2f}"


def check(rules: list[Rule], quotes: dict[str, dict[str, Any]], now: dt.datetime | None,
          state: dict[str, Any]) -> list[dict[str, Any]]:
    """Rules that ring now. Each rings once (daily types once per day); `state` is updated in place."""
    t = market.now_ist(now)
    today = t.date().isoformat()
    fired_state = state.setdefault("fired", {})
    trading = market.is_trading_day(t.date())
    out: list[dict[str, Any]] = []
    for r in rules:
        rid, typ = r.get("id"), r.get("type")
        if r.get("date") and r["date"] != today:
            continue
        key = f"{rid}@{today}" if typ in DAILY_TYPES else rid
        if key in fired_state:
            continue
        q = quotes.get(yahoo(r["ticker"])) if r.get("ticker") else None
        price = q.get("price") if q else None
        msg = None
        name = symbol(r["ticker"]) if r.get("ticker") else "NSE"
        if typ in ("above", "target") and price is not None and price >= r["price"]:
            msg = f"{name} at {_rs(price)}, at or above {_rs(r['price'])}"
        elif typ in ("below", "stop") and price is not None and price <= r["price"]:
            msg = f"{name} at {_rs(price)}, at or below {_rs(r['price'])}"
        elif typ == "pct_move" and trading and price is not None and q.get("prev_close"):
            move = (price / q["prev_close"] - 1) * 100
            d = r.get("direction", "any")
            if abs(move) >= r["pct"] and (d == "any" or (d == "up") == (move > 0)):
                msg = f"{name} moved {move:+.1f}% from the previous close {_rs(q['prev_close'])} to {_rs(price)}"
        elif typ == "time" and trading:
            hh, mm = map(int, r["at"].split(":"))
            if t.time() >= dt.time(hh, mm):
                msg = f"{t:%H:%M} IST: past {r['at']} IST" + (f" for {name}" if r.get("ticker") else "")
        elif typ == "opening_bell" and trading and t.time() >= market.OPEN:
            msg = f"NSE opened today at {market.OPEN:%H:%M} IST"
        if msg is None:
            continue
        if r.get("note"):
            msg += f" ({r['note']})"
        fired_state[key] = t.isoformat(timespec="seconds")
        out.append({"id": rid, "type": typ, "ticker": r.get("ticker"), "price": price,
                    "at": t.isoformat(timespec="seconds"), "message": msg})
    return out


# ---------------------------------------------------------------- quotes and sending

def fetch_quotes(tickers: list[str], fetch: Fetch | None = None, pause: float = 0.3) -> dict[str, dict[str, Any]]:
    """{ticker: {"price", "prev_close", "time"}} from Yahoo's chart JSON. Never raises; missing tickers are skipped."""
    out: dict[str, dict[str, Any]] = {}
    for i, raw in enumerate(dict.fromkeys(yahoo(t) for t in tickers)):
        if i and pause and fetch is None:
            time.sleep(pause)   # be polite to a free endpoint
        try:
            res = (get_json(CHART_URL.format(t=raw), fetch).get("chart") or {}).get("result") or []
            meta = res[0]["meta"]
            price = meta.get("regularMarketPrice")
            if price is None:
                continue
            prev = meta.get("previousClose") or meta.get("chartPreviousClose")
            ts = meta.get("regularMarketTime")
            out[raw] = {"price": float(price), "prev_close": float(prev) if prev else None,
                        "time": (dt.datetime.fromtimestamp(ts, market.IST).isoformat(timespec="seconds")
                                 if ts else None)}
        except Exception as exc:  # network, 404 for an unknown ticker, odd JSON
            log.info("no quote for %s: %s", raw, exc)
    return out


def format_fired(fired: list[dict[str, Any]]) -> str:
    lines = ["PaperTrader alerts (PAPER MONEY) - information only, not orders:"]
    lines += [f"- {f['message']}" for f in fired]
    return "\n".join(lines)


def send(fired: list[dict[str, Any]], notifier: Callable[[str], bool] | None = None) -> bool:
    """Send the fired alerts as one message. Nothing to send -> False without calling the notifier."""
    if not fired:
        return False
    if notifier is None:
        from papertrader.notify import send_telegram
        notifier = send_telegram
    try:
        return bool(notifier(format_fired(fired)))
    except Exception as exc:
        log.warning("alert notification failed: %s", exc)
        return False
