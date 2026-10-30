"""Go-live preflight: an automatic checklist that prints PASS / FAIL per item.

Usage:
    python scripts/preflight.py                 # run every check, write data/live/preflight.json
    python scripts/preflight.py --skip-tests    # faster; the "tests" item then FAILS
    python scripts/preflight.py --min-paper-days 60

The live executor (papertrader.live.executor) refuses to trade unless the report it
writes is under 24 hours old and every item passed. Today it cannot pass: there is no
verified real broker adapter, and that item fails on purpose.

Environment it reads (never prints secrets):
    PAPERTRADER_STATIC_IP          the static IP registered with your broker
    PAPERTRADER_LIVE_CAPITAL_CAP   capital cap in INR (default 25000)
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   alerts
    PAPERTRADER_KILL_SWITCH        must be off

Exit codes: 0 = all PASS, 1 = at least one FAIL.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from papertrader.agent.market import now_ist  # noqa: E402
from papertrader.config import PROJECT_ROOT, Config, load_config  # noqa: E402
from papertrader.live.executor import (DEFAULT_CAPITAL_CAP, DEFAULT_PREFLIGHT_PATH,  # noqa: E402
                                       DEFAULT_RECONCILE_PATH, kill_switch_on)
from papertrader.live.interface import BrokerSession  # noqa: E402
from papertrader.live.simulated import SimulatedExchange  # noqa: E402

STAGE_C_CAP = 25_000.0
MAX_CLOCK_SKEW_S = 5.0
MAX_DRAWDOWN_PCT = 20.0          # paper results worse than this are "catastrophic"
MIN_TOTAL_RETURN_PCT = -10.0
RECONCILE_MAX_AGE_DAYS = 3
IP_URL = "https://api.ipify.org"
TIME_URL = "https://www.google.com"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        return f"[{'PASS' if self.ok else 'FAIL'}] {self.name:<22} {self.detail}"


# ----- default probes (network / subprocess); tests replace them -----

def run_test_suite() -> tuple[bool, str]:
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=PROJECT_ROOT,
                          capture_output=True, text=True, timeout=1800)
    tail = (proc.stdout or proc.stderr).strip().splitlines()
    return proc.returncode == 0, tail[-1] if tail else f"exit code {proc.returncode}"


def fetch_public_ip(timeout: float = 10.0) -> str:
    with urllib.request.urlopen(IP_URL, timeout=timeout) as resp:
        return resp.read().decode().strip()


def fetch_server_time(timeout: float = 10.0) -> tuple[dt.datetime, dt.datetime]:
    """(server time from the HTTP Date header, local time at the middle of the request)."""
    req = urllib.request.Request(TIME_URL, method="HEAD")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        header = resp.headers.get("Date")
    t1 = time.time()
    if not header:
        raise ValueError(f"{TIME_URL} sent no Date header")
    local_mid = dt.datetime.fromtimestamp((t0 + t1) / 2, dt.timezone.utc)
    return email.utils.parsedate_to_datetime(header), local_mid


def default_session() -> BrokerSession:
    """No real adapter exists yet, so the preflight rehearses against the SimulatedExchange."""
    ex = SimulatedExchange()
    ex.login()
    return ex


def paper_snapshots(cfg: Config) -> list[dict[str, Any]]:
    from papertrader.storage.sqlite_store import SQLiteStore
    path = cfg.storage.sqlite_path
    if not Path(path).exists():
        return []
    store = SQLiteStore(path)
    try:
        return store.rows("daily_snapshots", order_by="as_of_date")
    finally:
        store.close()


# ----- checks -----

def check_tests(run_tests: Callable[[], tuple[bool, str]] | None) -> CheckResult:
    if run_tests is None:
        return CheckResult("tests", False, "skipped (--skip-tests); go-live needs a green suite")
    try:
        ok, detail = run_tests()
    except Exception as exc:
        return CheckResult("tests", False, f"could not run: {exc}")
    return CheckResult("tests", ok, detail)


def check_mode(cfg: Config) -> CheckResult:
    mode = cfg.trading.mode
    if mode == "paper":
        return CheckResult("trading.mode", True, "paper (the live switch is not flipped; expected before Stage C)")
    return CheckResult("trading.mode", mode in ("shadow", "live"), f"{mode}")


def check_kill_switch(cfg: Config, env: Mapping[str, str]) -> list[CheckResult]:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "KILL_SWITCH"
        by_env = kill_switch_on({"PAPERTRADER_KILL_SWITCH": "1"}, f)
        f.write_text("stop", encoding="utf-8")
        by_file = kill_switch_on({}, f)
        f.unlink()
        clear = not kill_switch_on({}, f)
    tested = CheckResult("kill switch works", by_env and by_file and clear,
                         f"env trigger {'ok' if by_env else 'BROKEN'}, file trigger {'ok' if by_file else 'BROKEN'}, "
                         f"clears {'ok' if clear else 'BROKEN'}")
    on = kill_switch_on(env, cfg.trading.kill_switch_file)
    off = CheckResult("kill switch off", not on, "ON: remove it before trading" if on else "off")
    return [tested, off]


def check_capital_cap(env: Mapping[str, str]) -> CheckResult:
    raw = env.get("PAPERTRADER_LIVE_CAPITAL_CAP", "")
    try:
        cap = float(raw) if raw else DEFAULT_CAPITAL_CAP
    except ValueError:
        return CheckResult("capital cap", False, f"PAPERTRADER_LIVE_CAPITAL_CAP={raw!r} is not a number")
    ok = 0 < cap <= STAGE_C_CAP
    src = "env" if raw else "default"
    return CheckResult("capital cap", ok, f"Rs {cap:,.0f} ({src}); Stage C allows up to Rs {STAGE_C_CAP:,.0f}")


def check_static_ip(env: Mapping[str, str], fetch_ip: Callable[[], str]) -> CheckResult:
    expected = env.get("PAPERTRADER_STATIC_IP", "").strip()
    if not expected:
        return CheckResult("static IP", False, "PAPERTRADER_STATIC_IP not set (register a static IP with the broker)")
    try:
        actual = fetch_ip().strip()
    except Exception as exc:
        return CheckResult("static IP", False, f"could not fetch public IP: {exc}")
    return CheckResult("static IP", actual == expected,
                       f"public {actual} {'matches' if actual == expected else 'DOES NOT match'} expected {expected}")


def check_clock(fetch_time: Callable[[], tuple[dt.datetime, dt.datetime]]) -> CheckResult:
    try:
        server, local = fetch_time()
    except Exception as exc:
        return CheckResult("clock sync", False, f"could not read server time: {exc}")
    skew = (local - server).total_seconds()
    return CheckResult("clock sync", abs(skew) <= MAX_CLOCK_SKEW_S,
                       f"local clock off by {skew:+.1f} s (limit {MAX_CLOCK_SKEW_S:.0f} s; HTTP Date has 1 s resolution)")


def check_session(session_factory: Callable[[], BrokerSession], now: dt.datetime) -> list[CheckResult]:
    try:
        session = session_factory()
        status = session.session_status()
    except Exception as exc:
        return [CheckResult("broker session", False, f"could not open: {exc}"),
                CheckResult("broker adapter", False, "no session")]
    kind = type(session).__name__
    valid = status.valid_at(now)
    sess = CheckResult("broker session", valid, f"{kind}: " + ("logged in" + (f", expires {status.expires_at:%d %b %H:%M}"
                                                                              if status.expires_at else "")
                                                               if valid else (status.note or "not logged in")))
    verified = getattr(type(session), "verified", False) is True and not isinstance(session, SimulatedExchange)
    adapter = CheckResult("broker adapter", verified,
                          f"{kind} is verified" if verified else
                          f"{kind} is not a verified real adapter (see src/papertrader/live/brokers/README.md)")
    return [sess, adapter]


def check_paper_history(rows: list[dict[str, Any]], min_days: int) -> CheckResult:
    days = sorted({str(r.get("as_of_date"))[:10] for r in rows if r.get("as_of_date")})
    equity = [float(r["equity"]) for r in sorted(rows, key=lambda r: str(r.get("as_of_date")))
              if r.get("equity") not in (None, "")]
    if len(days) < min_days or not equity:
        return CheckResult("paper history", False, f"{len(days)} trading day(s) of daily_snapshots; need >= {min_days}")
    peak, mdd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak * 100 if peak else 0.0)
    total = (equity[-1] / equity[0] - 1) * 100 if equity[0] else 0.0
    ok = mdd <= MAX_DRAWDOWN_PCT and total >= MIN_TOTAL_RETURN_PCT
    return CheckResult("paper history", ok,
                       f"{len(days)} days {days[0]}..{days[-1]}, return {total:+.1f}%, max drawdown {mdd:.1f}% "
                       f"(fail if drawdown > {MAX_DRAWDOWN_PCT:.0f}% or return < {MIN_TOTAL_RETURN_PCT:.0f}%)")


def check_reconciliation(path: Path, now: dt.datetime) -> CheckResult:
    if not path.exists():
        return CheckResult("reconciliation", False, f"no shadow reconciliation report at {path.name} (run shadow mode)")
    try:
        rep = json.loads(path.read_text(encoding="utf-8"))
        as_of = dt.date.fromisoformat(str(rep["as_of"])[:10])
    except (OSError, ValueError, KeyError) as exc:
        return CheckResult("reconciliation", False, f"unreadable report: {exc}")
    age = (now.date() - as_of).days
    if age > RECONCILE_MAX_AGE_DAYS:
        return CheckResult("reconciliation", False, f"last report {as_of} is {age} days old")
    clean = rep.get("clean") is True
    return CheckResult("reconciliation", clean, f"{as_of}: " + ("clean" if clean else f"NOT clean (worst {rep.get('worst')})"))


def check_telegram(cfg: Config | None, env: Mapping[str, str]) -> CheckResult:
    have = bool(env.get("TELEGRAM_BOT_TOKEN")) and bool(env.get("TELEGRAM_CHAT_ID"))
    enabled = cfg is None or cfg.notify.telegram
    ok = have and enabled
    detail = ("token and chat id set" if have else "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing") + \
             ("" if enabled else "; notify.telegram is off in config.yaml")
    return CheckResult("telegram alerts", ok, detail)


# ----- runner -----

def run_preflight(*, cfg: Config | None = None, cfg_path: str | Path | None = None,
                  env: Mapping[str, str] | None = None, now: dt.datetime | None = None,
                  run_tests: Callable[[], tuple[bool, str]] | None = run_test_suite,
                  fetch_ip: Callable[[], str] = fetch_public_ip,
                  fetch_time: Callable[[], tuple[dt.datetime, dt.datetime]] = fetch_server_time,
                  session_factory: Callable[[], BrokerSession] = default_session,
                  snapshots: Callable[[Config], list[dict[str, Any]]] = paper_snapshots,
                  reconcile_path: Path | str | None = None, out_path: Path | str | None = None,
                  min_paper_days: int = 60) -> dict[str, Any]:
    """Run every check, write the JSON report and return it."""
    env = os.environ if env is None else env
    now = now_ist(now)
    results: list[CheckResult] = [check_tests(run_tests)]
    if cfg is None:
        try:
            cfg = load_config(cfg_path)
            results.append(CheckResult("config valid", True, "config.yaml loads and validates"))
        except Exception as exc:
            results.append(CheckResult("config valid", False, f"{type(exc).__name__}: {exc}"))
    else:
        results.append(CheckResult("config valid", True, "config supplied"))
    if cfg is not None:
        results.append(check_mode(cfg))
        results += check_kill_switch(cfg, env)
    results.append(check_capital_cap(env))
    results.append(check_static_ip(env, fetch_ip))
    results.append(check_clock(fetch_time))
    results += check_session(session_factory, now)
    if cfg is not None:
        try:
            results.append(check_paper_history(snapshots(cfg), min_paper_days))
        except Exception as exc:
            results.append(CheckResult("paper history", False, f"could not read the store: {exc}"))
    results.append(check_reconciliation(Path(reconcile_path or DEFAULT_RECONCILE_PATH), now))
    results.append(check_telegram(cfg, env))

    report = {"timestamp": now.isoformat(), "passed": all(r.ok for r in results),
              "results": [asdict(r) for r in results]}
    out = Path(out_path or DEFAULT_PREFLIGHT_PATH)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["path"] = str(out)
    report["lines"] = [r.line() for r in results]
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--skip-tests", action="store_true", help="do not run pytest (the tests item then fails)")
    ap.add_argument("--min-paper-days", type=int, default=60)
    args = ap.parse_args(argv)
    report = run_preflight(cfg_path=args.config, run_tests=None if args.skip_tests else run_test_suite,
                           min_paper_days=args.min_paper_days)
    print("PaperTrader go-live preflight - " + report["timestamp"])
    for line in report["lines"]:
        print(line)
    failed = sum(1 for r in report["results"] if not r["ok"])
    print(f"\n{'ALL PASS' if report['passed'] else f'{failed} FAIL(S)'}; report written to {report['path']}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
