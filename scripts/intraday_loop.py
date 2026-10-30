"""Intraday PAPER trading session (opening-range breakouts). PAPER ONLY - no broker, no real money.

Usage:
    python scripts/intraday_loop.py --once                 # one cycle (a timer runs this every 5 minutes)
    python scripts/intraday_loop.py --loop                 # cycle every 5 minutes until 15:30 IST, then summary
    python scripts/intraday_loop.py --once --dry-run       # same, but nothing real is written
    python scripts/intraday_loop.py --once --dry-run --now "2026-10-28 10:30"   # replay a past moment
    python scripts/intraday_loop.py --loop --dry-run --now "2026-10-28 09:20"   # replay a whole past day fast
    python scripts/intraday_loop.py --status               # health: last cycle, open positions, today's P&L
    python scripts/intraday_loop.py --summary              # print today's end-of-day summary
    python scripts/intraday_loop.py --once --sync          # also upsert the journal to Supabase agent_intraday

Settings come from the `intraday:` section of config.yaml (all optional; see
src/papertrader/agent/intraday_runner.py IntradayConfig for the defaults).
Telegram secrets come from .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).

Safe to run any number of times: each cycle is idempotent, and a lock file stops
two cycles running at once. Exit codes: 0 = ok / nothing to do, 1 = the cycle
reported an error, 3 = another cycle is still running.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from papertrader.agent import market  # noqa: E402
from papertrader.agent.intraday_runner import (DEFAULT_STATE_DIR, IntradayConfig, IntradaySession,  # noqa: E402
                                               _kill_switch_on)
from papertrader.agent.market import CLOSE, IST  # noqa: E402

log = logging.getLogger("intraday_loop")
STEP = dt.timedelta(minutes=5)
LAG = dt.timedelta(seconds=30)        # wait a little past each 5-minute mark so Yahoo has the closed bar
LOCK_STALE = dt.timedelta(minutes=10)


def load_intraday_config(path: str | None) -> IntradayConfig:
    import yaml

    cfg_path = Path(path or os.environ.get("PAPERTRADER_CONFIG") or ROOT / "config.yaml")
    raw = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return IntradayConfig.from_dict(raw.get("intraday") or {})


def parse_now(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return market.now_ist(dt.datetime.fromisoformat(value.strip().replace("T", " ")))


class Lock:
    """A lock file so overlapping timer runs never cycle at the same time. Stale after 10 minutes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def __enter__(self) -> "Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {dt.datetime.now(IST).isoformat()}".encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                age = time.time() - self.path.stat().st_mtime
                if age > LOCK_STALE.total_seconds():
                    self.path.unlink(missing_ok=True)
                    continue
                return self
        return self

    def refresh(self) -> None:
        """A long --loop keeps its lock fresh so a timer run never treats it as stale."""
        if self.held:
            try:
                os.utime(self.path)
            except OSError:
                pass

    def __exit__(self, *exc) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)


def next_boundary(now: dt.datetime) -> dt.datetime:
    base = now.replace(second=0, microsecond=0)
    base -= dt.timedelta(minutes=base.minute % 5)
    nxt = base + STEP + LAG
    return nxt if nxt > now else nxt + STEP


def sync_journal(session: IntradaySession, day: dt.date | None = None, config_path: str | None = None) -> int:
    """Upsert the day's journal rows into the agent_intraday table (Supabase via the Store, else SQLite).

    Idempotent (rows carry a stable id). Never raises: a failed sync is logged and retried next cycle.
    """
    if session.dry_run:
        return 0
    try:
        from papertrader.config import load_config
        from papertrader.storage import Store, upsert

        rows = session.journal_rows(day or session.now().date())
        if not rows:
            return 0
        Store.from_config(load_config(config_path)).write([upsert("agent_intraday", rows)])
        return len(rows)
    except Exception as exc:  # the cycle already happened; sync again next time
        log.warning("journal sync to agent_intraday failed: %s", exc)
        return -1


def print_report(report, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print(report.describe())


def run_loop(session: IntradaySession, start: dt.datetime | None, as_json: bool, lock: Lock | None = None,
             sync: bool = False, config_path: str | None = None) -> int:
    """Cycle every 5 minutes until the close, then the summary. A replay clock (start) runs without sleeping."""
    clock = start
    worst = 0
    while True:
        session.set_now(clock)
        now = session.now()
        if lock:
            lock.refresh()
        report = session.cycle()
        print_report(report, as_json)
        if sync and report.status != "noop":
            sync_journal(session, now.date(), config_path)
        worst = max(worst, 1 if report.status == "error" else 0)
        if report.phase == "post_close" or not market.is_trading_day(now.date()) or now.time() >= CLOSE:
            return worst
        if report.phase in ("closed", "pre_open"):
            wake = dt.datetime.combine(now.date(), market.OPEN, IST) + LAG
        else:
            wake = next_boundary(now)
        if clock is not None:
            clock = wake                      # replay: jump straight to the next mark
        else:
            pause = (wake - market.now_ist()).total_seconds()
            if pause > 0:
                time.sleep(pause)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run one cycle and exit")
    mode.add_argument("--loop", action="store_true", help="cycle every 5 minutes until 15:30 IST")
    mode.add_argument("--status", action="store_true", help="print the health check and exit")
    mode.add_argument("--summary", action="store_true", help="print the day's summary and exit")
    ap.add_argument("--dry-run", action="store_true", help="work on a throwaway copy: no real ledger/journal writes, "
                                                           "no Telegram (messages are printed)")
    ap.add_argument("--now", help='replay clock, IST: "YYYY-MM-DD HH:MM"')
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--state-dir", help=f"where state, ledger and journal live (default {DEFAULT_STATE_DIR})")
    ap.add_argument("--json", action="store_true", help="print reports as JSON")
    ap.add_argument("--sync", action="store_true", help="after each cycle, upsert today's journal rows into the "
                                                        "agent_intraday table (ignored with --dry-run)")
    ap.add_argument("--quiet", action="store_true", help="warnings only in the log")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    from papertrader.logs import setup_logging
    setup_logging(ROOT / "logs", logging.WARNING if args.quiet else logging.INFO)

    cfg = load_intraday_config(args.config)
    now = parse_now(args.now)
    notifier = (lambda text: print("\n[notify]\n" + text + "\n")) if args.dry_run else None
    session = IntradaySession(cfg, now=now, notifier=notifier, state_dir=args.state_dir, dry_run=args.dry_run,
                               kill_switch=lambda: _kill_switch_on(args.config))

    if args.status:
        st = session.status()
        if args.json:
            print(json.dumps(st, indent=2, default=str))
        else:
            print(f"Intraday PAPER status {st['day']} (state in {st['state_dir']})")
            print(f"  last cycle: {st['last_cycle'] or 'none today'} ({st['cycles']} cycle(s))")
            print(f"  trades today: {st['trades_today']}, realised P&L today Rs {st['realised_pnl_today']:,.2f}")
            print(f"  account equity Rs {st['equity']:,.2f}, cash Rs {st['cash']:,.2f}")
            for p in st["open_positions"]:
                print(f"  OPEN {p['qty']} {p['ticker']} from Rs {p['entry_price']:,.2f} stop Rs {p['stop_loss']:,.2f} "
                      f"target Rs {p['target']:,.2f} ({p['ticket_id']})")
            for r in st["last_rows"]:
                print(f"  {r['ts'][11:16]} [{r['event']}] {r['ticker']}: {r['reason'][:120]}")
        return 0
    if args.summary:
        print(session.daily_summary())
        return 0

    state_dir = Path(args.state_dir) if args.state_dir else DEFAULT_STATE_DIR
    lock_name = "intraday.dry.lock" if args.dry_run else "intraday.lock"
    with Lock(state_dir / lock_name) as lock:
        if not lock.held:
            print("Another intraday cycle is still running; skipping this one.")
            return 3
        if args.once:
            report = session.cycle()
            print_report(report, args.json)
            if args.sync and report.status != "noop":
                n = sync_journal(session, session.now().date(), args.config)
                print(f"  sync: {'failed (see log)' if n < 0 else f'{n} journal row(s) upserted to agent_intraday'}")
            return 1 if report.status == "error" else 0
        return run_loop(session, now, args.json, lock, args.sync, args.config)


if __name__ == "__main__":
    sys.exit(main())
