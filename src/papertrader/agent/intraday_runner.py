"""Autonomous intraday PAPER session: opening-range breakouts, start to finish. PAPER TRADING ONLY.

One `IntradaySession.cycle()` is one look at the market, meant to run every 5
minutes (systemd timer, Task Scheduler, or `scripts/intraday_loop.py --loop`):

  1. Market closed, a holiday or pre-open: nothing happens, nothing is written.
  2. Open paper positions are managed first, on COMPLETED 5-minute bars after
     the entry: a bar whose low reaches the stop exits at the stop (at the bar's
     open when it gapped through), a bar whose high reaches the target exits at
     the target (at the open on a gap up). A bar that touches both counts as the
     stop. At the flat-by time (15:15 IST) anything still open is closed at the
     close of the last bar that ended by then.
  3. Daily loss guard: once today's realised loss reaches `max_daily_loss`, no
     new entries for the rest of the day (exits keep running).
  4. Inside the entry window (09:20-14:30 IST): scan the watchlist for
     opening-range breakouts; for each new long trigger not yet handled today:
     events and news gates (cached per day), exit plan, time-machine odds gate,
     position size after NSE intraday charges, bracket ticket, risk check, and on
     PASS an automatic paper "yes" plus a paper fill at the last completed close.
  5. After 15:30 IST: one end-of-day summary (Telegram when configured).

Every decision, taken or skipped, is one row in data/agent/intraday_journal.jsonl.

Restart-safe and idempotent: what was decided today lives in
data/agent/intraday_state_YYYY-MM-DD.json and fills live in a separate
intraday paper ledger (data/agent/intraday_ledger.json, its own Rs 10 lakh
account), so running the same cycle twice never trades twice. Only completed
bars are used, so a replay clock (`now=`) never sees the future.

`mode` must be "paper": anything else raises LiveTradingRefused. There is no
broker code anywhere in this module.
"""
from __future__ import annotations

import atexit
import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from papertrader.agent import entry as entry_mod
from papertrader.agent import market
from papertrader.agent.budget import bracket_ticket, size_position
from papertrader.agent.entry import check_orb
from papertrader.agent.exitplan import PlanError, plan_trade
from papertrader.agent.market import IST
from papertrader.agent.riskcheck import RiskLimits, check_ticket
from papertrader.agent.scanner import scan_orb
from papertrader.agent.tickets import (approve, close_paper, execute_paper, load_ledger, refuse_live)
from papertrader.agent.types import BLOCK, UNKNOWN, Finding, symbol, yahoo
from papertrader.config import PROJECT_ROOT
from papertrader.costs import india_intraday
from papertrader.exits import intraday_exit

log = logging.getLogger(__name__)

BAR_MINUTES = 5
BAR = dt.timedelta(minutes=BAR_MINUTES)
DEFAULT_STATE_DIR = PROJECT_ROOT / "data" / "agent"
JOURNAL_NAME = "intraday_journal.jsonl"
LEDGER_NAME = "intraday_ledger.json"
EVENTS = ("skip", "entry", "exit", "flatten", "block", "error")
WATCHLISTS = ("config", "nifty50", "nifty100", "nifty500")
MAX_ERRORS_PER_TICKER = 3
HISTORY_DAYS = 59                     # Yahoo keeps about 60 days of 5-minute bars

BarsLoader = Callable[[str, int, dt.datetime], pd.DataFrame]   # (ticker, days, now) -> 5m bars
NewsFn = Callable[[str, dt.datetime], Finding]                  # (ticker, now) -> news finding
EventsFn = Callable[[str, dt.date], Finding]                    # (ticker, day) -> events finding
UniverseFn = Callable[[str], list]                              # watchlist name -> Yahoo tickers
Notifier = Callable[[str], Any]


def _clock(v: Any) -> dt.time:
    if isinstance(v, dt.time):
        return v
    return dt.datetime.strptime(str(v).strip(), "%H:%M").time()


def _rs(x: float | None) -> str:
    return "-" if x is None else f"Rs {x:,.2f}"


# ---------------------------------------------------------------- config

@dataclass
class IntradayConfig:
    """Settings for the intraday paper session. `from_dict` reads config.yaml's `intraday:` section."""
    enabled: bool = True
    mode: str = "paper"                          # paper only; anything else is refused
    watchlist: str = "nifty50"                   # config | nifty50 | nifty100 | nifty500
    tickers: list[str] = field(default_factory=list)   # explicit list; overrides `watchlist` when given
    or_minutes: int = 15
    stop_method: str = "or_mid"                  # or_mid | or_low | pct
    stop_pct: float = 1.0                        # used by stop_method "pct"
    target_r: float = 1.5
    max_loss_per_trade: float = 1_000.0
    max_daily_loss: float = 3_000.0
    max_trades_per_day: int = 3
    min_reward_risk: float = 1.0
    max_position_pct: float = 20.0
    account: float = 1_000_000.0                 # the separate intraday paper account (Rs)
    entry_start: dt.time = dt.time(9, 20)
    entry_end: dt.time = dt.time(14, 30)
    flat_by: dt.time = dt.time(15, 15)
    max_entry_delay_minutes: int = 15            # a trigger older than this is a different trade: skipped
    min_p_target_minus_p_stop: float | None = 0.0   # time-machine gate; None turns it off
    require_time_machine: bool = True            # skip when the time machine cannot run (too little history)
    tm_paths: int = 2000
    tm_seed: int = 19
    use_news_gate: bool = True
    news_hours: int = 48
    use_events_gate: bool = True
    block_on_unknown: bool = False               # True: a gate that cannot read its data blocks the entry
    auto_approve_paper: bool = True              # paper only: the session types the "yes" itself
    notify: bool = True
    max_symbols_per_cycle: int = 5               # most new triggers taken through the gates in one cycle
    fetch_workers: int = 8

    def __post_init__(self) -> None:
        refuse_live(self.mode)
        self.watchlist = str(self.watchlist).strip().lower().replace("_", "").replace(" ", "")
        if self.watchlist not in WATCHLISTS:
            raise ValueError(f"intraday.watchlist must be one of {', '.join(WATCHLISTS)}")
        if self.stop_method not in ("or_mid", "or_low", "pct"):
            raise ValueError("intraday.stop_method must be or_mid, or_low or pct")
        for name in ("account", "max_loss_per_trade", "max_daily_loss", "max_trades_per_day"):
            v = getattr(self, name)
            try:
                ok = not isinstance(v, bool) and float(v) > 0
            except (TypeError, ValueError):
                ok = False
            if not ok:
                raise ValueError(f"intraday.{name} must be a positive number, got {v!r}")
        for name in ("entry_start", "entry_end", "flat_by"):
            setattr(self, name, _clock(getattr(self, name)))
        if not self.entry_start < self.entry_end <= self.flat_by:
            raise ValueError("intraday times must satisfy entry_start < entry_end <= flat_by")
        self.tickers = [yahoo(t) for t in (self.tickers or [])]

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "IntradayConfig":
        """Build from a plain dict (config.yaml `intraday:`). Unknown keys raise ValueError (a typo
        such as `max_daily_los` must not silently fall back to a default)."""
        d = dict(d or {})
        names = {f.name for f in fields(cls)}
        extra = sorted(set(d) - names)
        if extra:
            raise ValueError(f"intraday config: unknown key(s) {', '.join(extra)}; "
                             f"known keys are {', '.join(sorted(names))}")
        return cls(**d)

    def limits(self) -> RiskLimits:
        return RiskLimits(max_loss_per_trade=self.max_loss_per_trade, max_daily_loss=self.max_daily_loss,
                          max_trades_per_day=self.max_trades_per_day, min_reward_risk=self.min_reward_risk,
                          max_position_pct=self.max_position_pct, entry_start=self.entry_start,
                          entry_end=self.entry_end)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("entry_start", "entry_end", "flat_by"):
            d[k] = d[k].strftime("%H:%M")
        return d


# ---------------------------------------------------------------- report

@dataclass
class CycleReport:
    """What one cycle saw and did."""
    at: str                                   # IST ISO time of the cycle clock
    day: str
    phase: str                                # closed | pre_open | open | post_close
    status: str                               # noop | ok | error
    dry_run: bool = False
    scanned: int = 0
    triggered: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)       # journal rows written this cycle
    open_positions: list[dict[str, Any]] = field(default_factory=list)
    trades_today: int = 0
    realised_pnl_today: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def describe(self) -> str:
        head = (f"Intraday PAPER cycle {self.at[:16].replace('T', ' ')} IST [{self.phase}] {self.status}"
                + (" (dry run)" if self.dry_run else ""))
        lines = [head]
        lines += [f"  note: {n}" for n in self.notes]
        if self.scanned:
            lines.append(f"  scanned {self.scanned} stocks, {self.triggered} long trigger(s)")
        for r in self.rows:
            px = f" @ {_rs(r['price'])}" if r.get("price") else ""
            qty = f" x{r['qty']}" if r.get("qty") else ""
            pnl = f" P&L {_rs(r['pnl'])}" if r.get("pnl") is not None else ""
            lines.append(f"  [{r['event']}] {symbol(r['ticker']) if r['ticker'] else '-'}{qty}{px}{pnl}: {r['reason']}")
        for p in self.open_positions:
            lines.append(f"  open: {p['qty']} {symbol(p['ticker'])} from {_rs(p['entry_price'])}, "
                         f"stop {_rs(p['stop_loss'])}, target {_rs(p['target'])} ({p['ticket_id']})")
        lines.append(f"  today: {self.trades_today} trade(s), realised {_rs(self.realised_pnl_today)}")
        return "\n".join(lines)


# ---------------------------------------------------------------- default data hooks

def default_bars_loader(ticker: str, days: int, now: dt.datetime) -> pd.DataFrame:
    from papertrader.agent.intraday import load_intraday
    return load_intraday(ticker, "5m", days=days, now=now)


def default_news(ticker: str, now: dt.datetime, hours: int = 48) -> Finding:
    from papertrader.agent.news import news_check
    return news_check(ticker, hours=hours, now=now)


def default_events(ticker: str, day: dt.date) -> Finding:
    from papertrader.agent.events import blackout_check
    return blackout_check(ticker, on=day)


def default_universe(name: str) -> list[str]:
    if name == "config":
        import yaml
        raw = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        return [yahoo(t) for t in raw.get("universe") or []]
    from papertrader.agent.universe import load_universe
    return load_universe(name)


KILL_SWITCH_ENV = "PAPERTRADER_KILL_SWITCH"
KILL_SWITCH_ON = ("1", "true", "yes", "on")


def kill_switch_file(config_path: str | Path | None = None) -> Path:
    """config.yaml `trading.kill_switch_file` (via papertrader.config.load_config), else PROJECT_ROOT/KILL_SWITCH."""
    try:
        from papertrader.config import load_config
        path = getattr(getattr(load_config(config_path), "trading", None), "kill_switch_file", None)
        if path:
            return Path(path)
    except Exception as exc:  # a broken config must not hide the default switch
        log.warning("kill switch: could not read trading.kill_switch_file (%s); using KILL_SWITCH", exc)
    return PROJECT_ROOT / "KILL_SWITCH"


def _kill_switch_on(config_path: str | Path | None = None) -> bool:
    """On when env PAPERTRADER_KILL_SWITCH is 1/true/yes/on (any case) or the configured file exists."""
    if os.environ.get(KILL_SWITCH_ENV, "").strip().lower() in KILL_SWITCH_ON:
        return True
    return kill_switch_file(config_path).exists()


# ---------------------------------------------------------------- bars

def completed_bars(bars: pd.DataFrame | None, now: dt.datetime) -> pd.DataFrame:
    """5-minute bars that had closed by `now` (start + 5 min <= now), IST index."""
    if bars is None or len(bars) == 0:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    b = entry_mod.as_ist_index(bars)
    b = b[~b.index.duplicated(keep="last")].sort_index()
    return entry_mod.completed(b, now, BAR_MINUTES)


def resolve_exit(rec: dict[str, Any], bars: pd.DataFrame, now: dt.datetime,
                 flat_by: dt.time) -> tuple[float, str, dt.datetime] | None:
    """(price, reason, when) for an open long paper trade, or None while it stays open.

    Bars that END after the entry are checked in order up to the flat-by time, by
    exits.intraday_exit: the stop before the target, gaps fill at the bar's open.
    The entry time is the END of the bar whose close was the fill, so every later
    bar is checked (none can slip between the fill and the cycle that made it).
    """
    entry_t = dt.datetime.fromisoformat(rec["entry_time"]).astimezone(IST)
    day = entry_t.date()
    flat_dt = dt.datetime.combine(day, flat_by, IST)
    stop, target = float(rec["stop_loss"]), float(rec["target"])
    b = entry_mod.session_bars(completed_bars(bars, now), day)
    if not b.empty:
        ends = b.index + BAR
        live = b[(ends > entry_t) & (ends <= flat_dt)]
        for start, bar in live.iterrows():
            hit = intraday_exit(float(bar["Open"]), float(bar["High"]), float(bar["Low"]), stop, target)
            if hit is not None:
                price, why = hit
                return price, _EXIT_REASON[why], min(start + BAR, now)
    if now >= flat_dt:
        if b.empty:
            return None
        before = b[b.index + BAR <= flat_dt]
        last = before if not before.empty else b
        return float(last["Close"].iloc[-1]), "flat_by", min(flat_dt, now)
    return None


_EXIT_REASON = {"stop_loss": "stop", "take_profit": "target"}   # exits.intraday_exit -> journal words


# ---------------------------------------------------------------- the session

class IntradaySession:
    """One intraday paper-trading day. Call `cycle()` every 5 minutes; it is safe to repeat."""

    def __init__(self, cfg: IntradayConfig | None = None, now: dt.datetime | None = None, *,
                 bars_loader: BarsLoader | None = None, news_fn: NewsFn | None = None,
                 events_fn: EventsFn | None = None, universe_fn: UniverseFn | None = None,
                 notifier: Notifier | None = None, state_dir: str | Path | None = None,
                 dry_run: bool = False, kill_switch: Callable[[], bool] | None = None) -> None:
        self.cfg = cfg or IntradayConfig()
        refuse_live(self.cfg.mode)
        self._fixed_now = now
        self.bars_loader = bars_loader or default_bars_loader
        self.news_fn = news_fn or (lambda t, n: default_news(t, n, self.cfg.news_hours))
        self.events_fn = events_fn or default_events
        self.universe_fn = universe_fn or default_universe
        self.kill_switch = kill_switch or _kill_switch_on
        self.dry_run = dry_run
        real_dir = Path(state_dir) if state_dir else DEFAULT_STATE_DIR
        if dry_run:
            # Work on a throwaway copy: same open positions and decisions, but nothing real is written.
            tmp = Path(tempfile.mkdtemp(prefix="papertrader_dry_"))
            atexit.register(shutil.rmtree, tmp, True)
            for name in (JOURNAL_NAME, LEDGER_NAME):
                if (real_dir / name).exists():
                    shutil.copy2(real_dir / name, tmp / name)
            for p in real_dir.glob("intraday_state_*.json") if real_dir.exists() else []:
                shutil.copy2(p, tmp / p.name)
            self.state_dir = tmp
            self.notifier = notifier            # tests may still inject one; the CLI passes print
        else:
            self.state_dir = real_dir
            self.notifier = notifier if notifier is not None else (_telegram if self.cfg.notify else None)
        self.journal_path = self.state_dir / JOURNAL_NAME
        self.ledger_path = self.state_dir / LEDGER_NAME
        self._bars: dict[tuple[str, int], pd.DataFrame] = {}

    # ------------------------------------------------------------ clock and files

    def now(self) -> dt.datetime:
        return market.now_ist(self._fixed_now)

    def set_now(self, now: dt.datetime | None) -> None:
        """Move the session clock (tests, replays, the loop)."""
        self._fixed_now = now
        self._bars.clear()

    def state_path(self, day: dt.date) -> Path:
        return self.state_dir / f"intraday_state_{day.isoformat()}.json"

    def load_state(self, day: dt.date) -> dict[str, Any]:
        base = {"day": day.isoformat(), "cycles": 0, "last_cycle": None, "decided": {}, "errors": {},
                "gates": {}, "entries": [], "guard_noted": False, "summary_sent": False, "universe": None}
        try:
            base.update(json.loads(self.state_path(day).read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("intraday state unreadable (%s); starting the day's state again", exc)
        return base

    def save_state(self, state: dict[str, Any]) -> None:
        _atomic_write(self.state_path(dt.date.fromisoformat(state["day"])), json.dumps(state, indent=2, default=str))

    def ledger(self) -> list[dict[str, Any]]:
        try:
            return load_ledger(self.ledger_path)
        except (OSError, ValueError) as exc:
            log.warning("intraday ledger unreadable: %s", exc)
            return []

    # ------------------------------------------------------------ journal

    def _journal(self, report: CycleReport, now: dt.datetime, event: str, ticker: str, reason: str, *,
                 price: float | None = None, qty: int | None = None, ticket_id: str | None = None,
                 pnl: float | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
        assert event in EVENTS
        ts = now.isoformat(timespec="seconds")
        day = now.date().isoformat()
        key = f"{day}|{ticker}|{event}|{ticket_id or ''}|{ts}|{reason}"
        row = {"id": hashlib.sha1(key.encode()).hexdigest()[:16], "ts": ts, "day": day, "ticker": ticker,
               "event": event, "reason": reason,
               "price": None if price is None else round(float(price), 2),
               "qty": None if qty is None else int(qty), "ticket_id": ticket_id,
               "pnl": None if pnl is None else round(float(pnl), 2),
               "details": json.loads(json.dumps(details or {}, default=str))}
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as exc:
            log.error("could not write the intraday journal: %s", exc)
        report.rows.append(row)
        return row

    def journal_rows(self, day: dt.date | str | None = None) -> list[dict[str, Any]]:
        """Journal rows (oldest first), all of them or one IST day's."""
        want = day.isoformat() if isinstance(day, dt.date) else day
        rows: list[dict[str, Any]] = []
        try:
            with open(self.journal_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if want is None or r.get("day") == want:
                        rows.append(r)
        except FileNotFoundError:
            pass
        return rows

    def _notify(self, text: str) -> None:
        if not self.notifier:
            return
        try:
            self.notifier(text)
        except Exception as exc:  # a failed message never fails a cycle
            log.warning("intraday notify failed: %s", exc)

    # ------------------------------------------------------------ data

    def _load(self, ticker: str, days: int, now: dt.datetime) -> pd.DataFrame:
        key = (ticker, days)
        if key not in self._bars:
            try:
                self._bars[key] = completed_bars(self.bars_loader(ticker, days, now), now)
            except Exception as exc:
                log.warning("bars for %s unavailable: %s", ticker, exc)
                self._bars[key] = completed_bars(None, now)
        return self._bars[key]

    def _scan_days(self, now: dt.datetime) -> int:
        """Enough days of bars to include `now`'s session (a replay of a past day needs more)."""
        back = (dt.datetime.now(IST).date() - now.date()).days
        return max(2, min(HISTORY_DAYS, back + 2))

    def _load_many(self, tickers: list[str], days: int, now: dt.datetime) -> dict[str, pd.DataFrame]:
        todo = [t for t in tickers if (t, days) not in self._bars]
        workers = max(1, min(self.cfg.fetch_workers, len(todo)))
        if todo and workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(lambda t: self._load(t, days, now), todo))
        return {t: self._load(t, days, now) for t in tickers}

    # ------------------------------------------------------------ money

    def account_view(self, ledger: list[dict[str, Any]], day: dt.date) -> dict[str, Any]:
        closed = [r for r in ledger if r.get("status") == "closed"]
        open_ = [r for r in ledger if r.get("status") == "open"]
        realised_all = sum(float(r.get("net_pnl") or 0.0) for r in closed)
        equity = self.cfg.account + realised_all
        in_use = sum(r["qty"] * r["entry_price"] + float(r.get("entry_charges") or 0.0) for r in open_)
        d = day.isoformat()
        realised_today = sum(float(r.get("net_pnl") or 0.0) for r in closed if (r.get("exit_time") or "")[:10] == d)
        trades_today = sum(1 for r in ledger if (r.get("entry_time") or "")[:10] == d)
        open_risk = sum(r["qty"] * max(0.0, r["entry_price"] - r["stop_loss"]) for r in open_)
        return {"equity": round(equity, 2), "cash": round(equity - in_use, 2), "open": open_,
                "realised_today": round(realised_today, 2), "trades_today": trades_today,
                "open_risk": round(open_risk, 2)}

    # ------------------------------------------------------------ the cycle

    def cycle(self) -> CycleReport:
        """One look at the market. Never raises for data problems; journals an "error" row instead."""
        now = self.now()
        status = market.market_status(now)
        day = now.date()
        report = CycleReport(at=now.isoformat(timespec="seconds"), day=day.isoformat(), phase=status.phase,
                             status="noop", dry_run=self.dry_run)
        if not market.is_trading_day(day) or status.phase in ("closed", "pre_open"):
            report.notes.append(status.describe())
            return report
        state = self.load_state(day)
        report.status = "ok"
        try:
            self._recover_entries(state, report, now)
            self._manage_positions(report, now)
            if status.phase == "post_close":
                self._end_of_day(state, report, now)
            else:
                self._entries(state, report, now)
        except Exception as exc:  # last line of defence: a cycle never crashes the timer
            log.exception("intraday cycle failed")
            report.status = "error"
            self._journal(report, now, "error", "", f"cycle failed: {exc}"[:300], details={"type": type(exc).__name__})
        view = self.account_view(self.ledger(), day)
        report.open_positions = view["open"]
        report.trades_today = view["trades_today"]
        report.realised_pnl_today = view["realised_today"]
        state["cycles"] = int(state.get("cycles", 0)) + 1
        state["last_cycle"] = report.at
        try:
            self.save_state(state)
        except OSError as exc:
            log.error("could not save intraday state: %s", exc)
        return report

    def _recover_entries(self, state: dict[str, Any], report: CycleReport, now: dt.datetime) -> None:
        """A crash between the paper fill and the state save: adopt today's fills from the ledger."""
        d = now.date().isoformat()
        for rec in self.ledger():
            if (rec.get("entry_time") or "")[:10] != d or rec["ticket_id"] in state["entries"]:
                continue
            state["entries"].append(rec["ticket_id"])
            state["decided"][rec["ticker"]] = "entry"
            logged = any(r.get("ticket_id") == rec["ticket_id"] and r.get("event") == "entry"
                         for r in self.journal_rows(d))
            if not logged:
                self._journal(report, now, "entry", rec["ticker"], "paper fill found in the ledger (recovered)",
                              price=rec["entry_price"], qty=rec["qty"], ticket_id=rec["ticket_id"])

    def _manage_positions(self, report: CycleReport, now: dt.datetime) -> None:
        for rec in [r for r in self.ledger() if r.get("status") == "open"]:
            ticker = rec["ticker"]
            try:
                entry_day = dt.datetime.fromisoformat(rec["entry_time"]).astimezone(IST).date()
                days = max(2, min(HISTORY_DAYS, (dt.datetime.now(IST).date() - entry_day).days + 2))
                bars = self._load(ticker, days, now)
                hit = resolve_exit(rec, bars, now, self.cfg.flat_by)
                if hit is None:
                    if now.date() > entry_day or now.time() >= self.cfg.flat_by:
                        self._journal(report, now, "error", ticker,
                                      "past the flat-by time but no bars to close at; will retry",
                                      ticket_id=rec["ticket_id"])
                    continue
                price, reason, when = hit
                done = close_paper(rec["ticket_id"], price, reason, now=when, ledger_path=self.ledger_path)
                event = "flatten" if reason == "flat_by" else "exit"
                words = {"stop": "stop-loss hit", "target": "target hit",
                         "flat_by": f"flat by {self.cfg.flat_by:%H:%M} IST"}[reason]
                self._journal(report, now, event, ticker, f"{words} at {when:%H:%M} IST",
                              price=done["exit_price"], qty=done["qty"], ticket_id=rec["ticket_id"],
                              pnl=done["net_pnl"], details={"r": done.get("r"), "exit_reason": reason,
                                                            "entry_price": done["entry_price"],
                                                            "exit_time": done["exit_time"]})
                self._notify(f"PAPER intraday {event.upper()}: {symbol(ticker)} {done['qty']} @ "
                             f"{_rs(done['exit_price'])} ({words}). Net P&L {_rs(done['net_pnl'])}"
                             f" ({done.get('r') or 0:+.2f}R before charges).")
            except Exception as exc:
                log.warning("managing %s failed: %s", ticker, exc)
                self._journal(report, now, "error", ticker, f"position check failed: {exc}"[:300],
                              ticket_id=rec.get("ticket_id"))

    def _end_of_day(self, state: dict[str, Any], report: CycleReport, now: dt.datetime) -> None:
        report.notes.append("Market closed for the day.")
        if state.get("summary_sent"):
            return
        text = self.daily_summary(now.date())
        report.notes.append("End-of-day summary written.")
        self._notify(text)
        state["summary_sent"] = True

    def _entries(self, state: dict[str, Any], report: CycleReport, now: dt.datetime) -> None:
        cfg = self.cfg
        if not cfg.enabled:
            report.notes.append("intraday.enabled is false: exits only, no new entries.")
            return
        if self.kill_switch():
            report.notes.append("Kill switch on: exits only, no new entries.")
            return
        view = self.account_view(self.ledger(), now.date())
        if view["realised_today"] <= -cfg.max_daily_loss:
            if not state.get("guard_noted"):
                self._journal(report, now, "block", "",
                              f"daily loss guard: lost {_rs(-view['realised_today'])} today, limit "
                              f"{_rs(cfg.max_daily_loss)}; no new entries today",
                              pnl=view["realised_today"])
                state["guard_noted"] = True
            report.notes.append("Daily loss limit reached: no new entries.")
            return
        t = now.time()
        if not (cfg.entry_start <= t <= cfg.entry_end):
            report.notes.append(f"Outside the entry window {cfg.entry_start:%H:%M}-{cfg.entry_end:%H:%M} IST.")
            return
        if view["trades_today"] >= cfg.max_trades_per_day:
            report.notes.append(f"{view['trades_today']} trade(s) today, the daily maximum.")
            return

        tickers = state.get("universe")
        if not tickers:
            try:
                tickers = cfg.tickers or list(self.universe_fn(cfg.watchlist))
            except Exception as exc:
                self._journal(report, now, "error", "", f"watchlist {cfg.watchlist} unavailable: {exc}"[:300])
                return
            state["universe"] = tickers
        bars = self._load_many(tickers, self._scan_days(now), now)
        report.scanned = sum(1 for b in bars.values() if not b.empty)
        hits = [h for h in scan_orb(bars, day=now.date(), at=now.time(), or_minutes=cfg.or_minutes,
                                    latest_entry=cfg.entry_end)
                if h["setup"] == "orb_long"]
        report.triggered = len(hits)

        taken = 0
        for hit in hits:
            ticker = hit["ticker"]
            if ticker in state["decided"]:
                continue
            if int(state["errors"].get(ticker, 0)) >= MAX_ERRORS_PER_TICKER:
                continue
            try:
                pre = self._precheck(ticker, bars[ticker], report, now)
                if isinstance(pre, str):
                    state["decided"][ticker] = pre
                    continue
                if taken >= cfg.max_symbols_per_cycle:
                    report.notes.append(f"More triggers than max_symbols_per_cycle ({cfg.max_symbols_per_cycle}); "
                                        "the rest wait for the next cycle.")
                    break
                taken += 1
                event = self._evaluate(ticker, pre, bars[ticker], state, report, now)
                if event:
                    state["decided"][ticker] = event
            except Exception as exc:
                log.warning("evaluating %s failed: %s", ticker, exc)
                state["errors"][ticker] = int(state["errors"].get(ticker, 0)) + 1
                self._journal(report, now, "error", ticker, f"evaluation failed: {exc}"[:300])
            if self.account_view(self.ledger(), now.date())["trades_today"] >= cfg.max_trades_per_day:
                break

    def _gate(self, kind: str, ticker: str, state: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
        cache = state["gates"].setdefault(ticker, {})
        if kind not in cache:
            f = self.news_fn(ticker, now) if kind == "news" else self.events_fn(ticker, now.date())
            cache[kind] = f.to_dict() if isinstance(f, Finding) else dict(f)
        return cache[kind]

    def _precheck(self, ticker: str, today_bars: pd.DataFrame, report: CycleReport,
                  now: dt.datetime) -> "str | entry_mod.EntryState":
        """Cheap checks first: already traded, the entry timer agrees, the trigger is fresh.
        Returns the EntryState to go on with, or the event already recorded."""
        cfg = self.cfg
        day = now.date()
        if any(r["ticker"] == ticker and (r.get("entry_time") or "")[:10] == day.isoformat() for r in self.ledger()):
            return "entry"
        es = check_orb(today_bars, day=day, at=now, or_minutes=cfg.or_minutes, latest_entry=cfg.entry_end)
        if not es.triggered:
            self._journal(report, now, "skip", ticker, f"entry timer says {es.status}: {es.reason}",
                          details={"entry": es.to_dict()})
            return "skip"
        delay = (now - es.trigger_time).total_seconds() / 60
        if delay > cfg.max_entry_delay_minutes:
            self._journal(report, now, "skip", ticker,
                          f"trigger at {es.trigger_time:%H:%M} IST is {delay:.0f} min old (limit "
                          f"{cfg.max_entry_delay_minutes}); a late entry is a different trade",
                          price=es.trigger_price, details={"entry": es.to_dict()})
            return "skip"
        return es

    def _evaluate(self, ticker: str, es: entry_mod.EntryState, today_bars: pd.DataFrame, state: dict[str, Any],
                  report: CycleReport, now: dt.datetime) -> str | None:
        """Take one fresh trigger through the gates, the plan, the odds, the size and the risk check.
        Returns the event recorded."""
        cfg = self.cfg
        day = now.date()
        gates: dict[str, Any] = {}
        for kind, on in (("events", cfg.use_events_gate), ("news", cfg.use_news_gate)):
            if not on:
                continue
            f = self._gate(kind, ticker, state, now)
            gates[kind] = {"status": f.get("status"), "summary": f.get("summary")}
            if f.get("status") == BLOCK or (cfg.block_on_unknown and f.get("status") == UNKNOWN):
                self._journal(report, now, "block", ticker, f"{kind} gate {f.get('status')}: {f.get('summary')}"[:400],
                              price=es.trigger_price, details={"gates": gates})
                return "block"

        entry_px = float(es.last_close or es.trigger_price)
        try:
            plan = plan_trade(ticker, entry_px, stop_method=cfg.stop_method, stop_pct=cfg.stop_pct,
                              range_high=es.range_high, range_low=es.range_low, target_r=cfg.target_r,
                              style="intraday", flat_by=cfg.flat_by)
        except PlanError as exc:
            self._journal(report, now, "skip", ticker, f"no exit plan: {exc}", price=entry_px,
                          details={"gates": gates, "entry": es.to_dict()})
            return "skip"

        bars = self._load(ticker, HISTORY_DAYS, now)
        today = entry_mod.session_bars(bars, day)
        if today.empty:
            today = today_bars[today_bars.index.date == day] if len(today_bars) else today_bars
        hist = bars[bars.index.date < day] if len(bars) else bars
        prev_close = float(hist["Close"].iloc[-1]) if len(hist) else None
        tm: dict[str, Any] = {}
        if cfg.min_p_target_minus_p_stop is not None:
            from papertrader.agent.timemachine import simulate_session
            tm = simulate_session(hist, today, plan, n=cfg.tm_paths, seed=cfg.tm_seed)
            tm_short = {k: tm.get(k) for k in ("ok", "p_target", "p_stop", "p_open", "median_end_r", "mean_r",
                                               "sessions_used", "reason", "verdict")}
            if not tm.get("ok"):
                if cfg.require_time_machine:
                    self._journal(report, now, "skip", ticker, f"time machine could not run: {tm.get('reason')}",
                                  price=plan.entry, details={"plan": plan.to_dict(), "gates": gates, "tm": tm_short})
                    return "skip"
            else:
                edge = tm["p_target"] - tm["p_stop"]
                if edge < cfg.min_p_target_minus_p_stop:
                    self._journal(report, now, "skip", ticker,
                                  f"time machine: target first {tm['p_target']:.1%} vs stop first {tm['p_stop']:.1%} "
                                  f"(edge {edge:+.1%}, need {cfg.min_p_target_minus_p_stop:+.1%})",
                                  price=plan.entry, details={"plan": plan.to_dict(), "gates": gates, "tm": tm_short})
                    return "skip"
            tm = tm_short

        view = self.account_view(self.ledger(), day)
        costs = india_intraday()
        sizing = size_position(plan, account=view["equity"], max_loss=cfg.max_loss_per_trade,
                               max_position_pct=cfg.max_position_pct, cash=view["cash"], costs=costs)
        details = {"plan": plan.to_dict(), "gates": gates, "tm": tm, "entry": es.to_dict(),
                   "sizing": {k: sizing[k] for k in ("qty", "notional", "loss_at_stop", "gain_at_target",
                                                     "binding", "reasons")}}
        if not sizing["ok"]:
            self._journal(report, now, "skip", ticker, "no size: " + " ".join(sizing["reasons"]),
                          price=plan.entry, details=details)
            return "skip"
        ticket = bracket_ticket(plan, sizing, mode=cfg.mode, now=now)
        # Open trades' loss at the stop counts as already lost, so all of today's trades
        # stopping out together still stays inside the daily limit.
        verdict = check_ticket(ticket, plan, limits=cfg.limits(), account=view["equity"], cash=view["cash"],
                               trades_today=view["trades_today"],
                               realised_pnl_today=view["realised_today"] - view["open_risk"],
                               now=now, prev_close=prev_close, costs=costs)
        details["risk"] = {"verdict": verdict.verdict, "reasons": verdict.reasons}
        if not verdict.passed:
            self._journal(report, now, "block", ticker, "risk check BLOCK: " + "; ".join(verdict.reasons),
                          price=plan.entry, qty=ticket.qty, ticket_id=ticket.ticket_id, details=details)
            return "block"
        if not cfg.auto_approve_paper:
            self._journal(report, now, "skip", ticker, "risk PASS but auto_approve_paper is off: ticket not filled",
                          price=plan.entry, qty=ticket.qty, ticket_id=ticket.ticket_id,
                          details={**details, "ticket": ticket.to_dict()})
            return "skip"
        approve(ticket, "yes", now=now)
        # The fill is the close of the last completed bar, so the trade's clock starts at that bar's
        # END, not at the wall clock: resolve_exit then checks every bar after it, including one that
        # closed between that bar and this cycle (a lagging download would otherwise hide its stop).
        fill_time = fill_bar_end(es, now)
        fill = execute_paper(ticket, entry_px, now=fill_time, ledger_path=self.ledger_path, costs=costs)
        state["entries"].append(ticket.ticket_id)
        details["ticket"] = ticket.to_dict()
        self._journal(report, now, "entry", ticker,
                      f"ORB long: {es.reason.split(' Nothing was placed.')[0]} Paper buy at {_rs(fill['entry_price'])}, "
                      f"stop {_rs(plan.stop)}, target {_rs(plan.target)}",
                      price=fill["entry_price"], qty=fill["qty"], ticket_id=ticket.ticket_id, details=details)
        tm_txt = (f" Time machine: target first {tm['p_target']:.0%}, stop first {tm['p_stop']:.0%}."
                  if tm.get("ok") else "")
        self._notify(f"PAPER intraday BUY {fill['qty']} {symbol(ticker)} @ {_rs(fill['entry_price'])} "
                     f"({now:%H:%M} IST). Stop {_rs(plan.stop)}, target {_rs(plan.target)}, out by "
                     f"{cfg.flat_by:%H:%M}. Loss at stop about {_rs(sizing['loss_at_stop'])}.{tm_txt} "
                     f"Ticket {ticket.ticket_id}. Paper money only.")
        return "entry"

    # ------------------------------------------------------------ words

    def daily_summary(self, day: dt.date | None = None) -> str:
        """End-of-day text for Telegram: trades, P&L, what was skipped and why."""
        day = day or self.now().date()
        d = day.isoformat()
        rows = self.journal_rows(d)
        ledger = self.ledger()
        trades = [r for r in ledger if (r.get("entry_time") or "")[:10] == d]
        closed = [r for r in trades if r.get("status") == "closed"]
        net = round(sum(float(r.get("net_pnl") or 0.0) for r in closed), 2)
        wins = sum(1 for r in closed if (r.get("net_pnl") or 0) > 0)
        view = self.account_view(ledger, day)
        lines = [f"PaperTrader intraday (PAPER MONEY) - {day:%a %d %b %Y}",
                 f"Trades: {len(trades)} ({wins} winner(s)); net P&L after charges {_rs(net)}.",
                 f"Intraday paper account: {_rs(view['equity'])} equity."]
        for r in trades:
            if r.get("status") == "closed":
                lines.append(f"- {symbol(r['ticker'])} {r['qty']} @ {_rs(r['entry_price'])} -> "
                             f"{_rs(r['exit_price'])} ({r['exit_reason']}), net {_rs(r['net_pnl'])}"
                             + (f", {r['r']:+.2f}R" if r.get("r") is not None else ""))
            else:
                lines.append(f"- {symbol(r['ticker'])} {r['qty']} @ {_rs(r['entry_price'])} STILL OPEN")
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["event"]] = counts.get(r["event"], 0) + 1
        skipped = [r for r in rows if r["event"] in ("skip", "block")]
        if skipped:
            lines.append(f"Passed on {len(skipped)} trigger(s):")
            for r in skipped[:10]:
                lines.append(f"- {symbol(r['ticker']) if r['ticker'] else '-'} [{r['event']}]: {r['reason'][:160]}")
            if len(skipped) > 10:
                lines.append(f"... and {len(skipped) - 10} more.")
        if counts.get("error"):
            lines.append(f"{counts['error']} data error(s); see data/agent/{JOURNAL_NAME}.")
        if not rows and not trades:
            lines.append("No opening-range breakout passed the scan today.")
        lines.append("Simulated trades only; nothing was sent to a broker.")
        return "\n".join(lines)

    def status(self, day: dt.date | None = None) -> dict[str, Any]:
        """Health check: last cycle, open positions, today's P&L."""
        day = day or self.now().date()
        state = self.load_state(day)
        view = self.account_view(self.ledger(), day)
        rows = self.journal_rows(day)
        return {"day": day.isoformat(), "last_cycle": state.get("last_cycle"), "cycles": state.get("cycles", 0),
                "open_positions": view["open"], "trades_today": view["trades_today"],
                "realised_pnl_today": view["realised_today"], "equity": view["equity"], "cash": view["cash"],
                "journal_rows_today": len(rows), "last_rows": rows[-5:], "state_dir": str(self.state_dir)}


# ---------------------------------------------------------------- helpers

def fill_bar_end(es: entry_mod.EntryState, now: dt.datetime) -> dt.datetime:
    """End time of the bar whose close is the paper fill (the last completed bar; the trigger bar
    when the trigger is fresh), never later than `now`."""
    if es.last_bar_start is not None:
        end = pd.Timestamp(es.last_bar_start).tz_convert(IST) + BAR
        return min(end.to_pydatetime(), now)
    if es.trigger_time is not None:
        return min(es.trigger_time, now)
    return now


def _telegram(text: str) -> bool:
    from papertrader.notify import send_telegram
    return send_telegram(text)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
