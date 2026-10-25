"""Prove the dashboard key is read-only.

Reads SUPABASE_URL and SUPABASE_ANON_KEY from .env, then for every table checks
that SELECT works and that INSERT / UPDATE / DELETE are refused.

    python scripts/check_rls.py
Exit code 0 = read-only as expected, 1 = a write got through (fix RLS!).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from supabase import create_client  # noqa: E402

from papertrader.storage.schema import TABLES  # noqa: E402

PROBE = "__rls_probe__"


def refused(fn) -> tuple[bool, str]:
    """True if the write raised, or silently matched no rows (RLS filtered it out)."""
    try:
        res = fn()
    except Exception as exc:
        return True, str(exc).splitlines()[0][:80]
    return (not res.data), f"returned {len(res.data or [])} row(s)"


def main() -> int:
    load_dotenv(ROOT / ".env")
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_ANON_KEY")
    if not url or not key:
        print("Set SUPABASE_URL and SUPABASE_ANON_KEY in .env first.")
        return 1
    client = create_client(url, key)
    ok = True
    for table, (pk, _) in TABLES.items():
        t = lambda: client.table(table)  # noqa: E731
        try:
            t().select(pk).limit(1).execute()
            read = "read OK"
        except Exception as exc:
            read, ok = f"READ FAILED ({exc})", False
        checks = {
            "insert": refused(lambda: t().insert({pk: PROBE}).execute()),
            "update": refused(lambda: t().update({pk: PROBE}).neq(pk, PROBE).execute()),
            "delete": refused(lambda: t().delete().neq(pk, PROBE).execute()),
        }
        bad = [k for k, (r, _) in checks.items() if not r]
        ok &= not bad
        status = "PASS" if not bad else f"FAIL: {', '.join(bad)} allowed"
        print(f"{table:<17} {read:<10} writes refused: {status}")
    print("\nDashboard key is READ-ONLY." if ok else "\nPROBLEM: the dashboard key can write. Re-run supabase/rls.sql.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
