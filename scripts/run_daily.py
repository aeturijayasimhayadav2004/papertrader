"""Daily paper-trading run. PAPER ONLY - no broker, no real money.

Usage:
    python scripts/run_daily.py            # process any new completed trading day(s)
    python scripts/run_daily.py --force    # recompute the latest day (overwrites, never duplicates)
    python scripts/run_daily.py --refresh  # ignore the price cache

Safe to run any number of times a day: an already-processed day is skipped.
Exit codes: 0 = ok / nothing to do, 1 = failed, 2 = aborted (state unavailable).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from papertrader.config import load_config  # noqa: E402
from papertrader.logs import setup_logging  # noqa: E402
from papertrader.runner import DailyRunner  # noqa: E402
from papertrader.storage import StateUnavailableError, Store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--force", action="store_true", help="recompute the latest trading day")
    ap.add_argument("--refresh", action="store_true", help="re-download prices, ignoring the cache")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log_path = setup_logging(cfg.log_dir)
    try:
        store = Store.from_config(cfg)
    except StateUnavailableError as exc:
        print(f"ABORTED: {exc}")
        return 2
    summary = DailyRunner(cfg, store).run(force=args.force, refresh=args.refresh)

    print("\n" + "=" * 70)
    print("PAPER TRADING - NO REAL MONEY")
    print(f"Status: {summary.status.upper()}  |  Latest bar: {summary.as_of_date}")
    print(summary.message)
    actionable = [d for d in summary.decisions if d["action"] not in ("HOLD",)]
    if actionable:
        print("\nNon-HOLD decisions on the last processed day:")
        for d in actionable:
            print(f"  {d['ticker']:<14} {d['strategy']:<13} {d['action']:<16} {d['reason']}")
    print(f"\nFull decision log: {log_path}")
    return {"ok": 0, "noop": 0, "aborted": 2}.get(summary.status, 1)


if __name__ == "__main__":
    sys.exit(main())
