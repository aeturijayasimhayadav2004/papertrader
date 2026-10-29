from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from papertrader.agent.http import FetchError
from papertrader.agent.insider import (
    bulk_block_deals, insider_check, insider_trades, parse_pit, rupees, summarise_trades,
)

FIX = Path(__file__).parent / "fixtures" / "agent" / "insider"
TODAY = dt.date(2026, 10, 29)
DEALS = {"historical/bulk-deals": "bulk_deals_acme.json", "historical/block-deals": "block_deals_acme.json"}


def router(routes: dict[str, str]):
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        for part, name in routes.items():
            if part in url:
                return (FIX / name).read_bytes()
        raise FetchError(f"{url}: HTTP Error 403: Forbidden")
    fetch.calls = calls
    return fetch


def yf_reliance(_t):
    """Real yfinance insider_transactions sample for RELIANCE.NS (values in USD)."""
    df = pd.read_csv(FIX / "yf_insiders_reliance.csv", parse_dates=["Start Date"])
    df.attrs["usd_inr"] = 88.0
    return df


def no_yahoo(_t):
    raise AssertionError("yfinance should not be needed")


def test_pit_url_and_window():
    fetch = router({"corporates-pit": "pit_acme_selling.json"})
    rows = insider_trades("ACME.NS", 30, fetch, TODAY, yf_loader=no_yahoo)
    assert "from_date=29-09-2026" in fetch.calls[0] and "to_date=29-10-2026" in fetch.calls[0]
    assert [r["date"] for r in rows] == ["2026-10-24", "2026-10-16", "2026-10-13"]   # July sale outside 30 days
    r = rows[0]
    assert (r["person"], r["category"], r["action"], r["qty"], r["value"]) == (
        "Acme Promoter Holdings Pvt Ltd", "Promoter Group", "sell", 1500000, 72000000.0)


def test_warn_on_net_promoter_selling_above_5_crore():
    f = insider_check("ACME.NS", 30, router({"corporates-pit": "pit_acme_selling.json", **DEALS}), TODAY,
                      yf_loader=no_yahoo)
    assert f.status == "warn"
    # the employee trust's Rs 1.44 cr sale is ignored; Rs 7.2 cr sold - Rs 9.6 lakh bought
    assert f.data["net_value"] == 960000 - 72000000
    assert "promoters net sold Rs 7.1 cr" in f.summary
    assert "2 bulk/block deals" in f.summary and len(f.data["deals"]) == 2


def test_selling_below_threshold_is_info():
    f = insider_check("ACME.NS", 30, router({"corporates-pit": "pit_acme_selling.json", **DEALS}), TODAY,
                      yf_loader=no_yahoo, threshold_rs=1e8)
    assert f.status == "info" and "below the Rs 10.0 cr alert level" in f.summary


def test_info_on_net_buying_ignores_esop():
    f = insider_check("ACME.NS", 30, router({"corporates-pit": "pit_acme_buying.json", **DEALS}), TODAY,
                      yf_loader=no_yahoo)
    assert f.status == "info"
    assert f.summary.startswith("ACME: promoters bought net Rs 1.2 cr")


def test_new_pledge_warns():
    f = insider_check("ACME.NS", 30, router({"corporates-pit": "pit_acme_pledge.json", **DEALS}), TODAY,
                      yf_loader=no_yahoo)
    assert f.status == "warn" and "pledged 4000000 shares on 2026-10-26" in f.summary


def test_nothing_is_ok():
    empty = {"corporates-pit": "block_deals_acme.json", "historical/": "block_deals_acme.json"}
    assert insider_check("ACME.NS", 30, router(empty), TODAY, yf_loader=no_yahoo).status == "ok"


def test_yfinance_fallback_when_nse_refuses_and_deals_skipped():
    rows = insider_trades("RELIANCE.NS", 365, router({}), TODAY, yf_loader=yf_reliance)
    sale = next(r for r in rows if r["person"] == "Narayan (B)")
    assert sale["action"] == "sell" and sale["value"] == 13104571 * 88.0 and sale["source"] == "yfinance"
    fetch = router({})
    f = insider_check("RELIANCE.NS", 365, fetch, TODAY, yf_loader=yf_reliance)
    assert f.status == "warn" and "Yahoo data, NSE unavailable" in f.summary
    assert len(fetch.calls) == 1              # one PIT try; no deal requests after the refusal
    assert insider_check("RELIANCE.NS", 30, router({}), TODAY, yf_loader=yf_reliance).status == "ok"


def test_bulk_block_deals_and_snapshot_fallback():
    deals = bulk_block_deals("ACME", 30, router(DEALS), TODAY)
    assert {d["action"] for d in deals} == {"buy", "sell"}
    assert next(d for d in deals if d["action"] == "sell")["value"] == 1500000 * 48.0
    snap = bulk_block_deals("ACME", 30, router({"largedeal": "largedeal_snapshot.json"}), TODAY)
    assert sorted((d["kind"], d["action"]) for d in snap) == [("block", "sell"), ("bulk", "buy")]   # OTHER dropped


def test_all_sources_fail_is_unknown():
    def boom(_t):
        raise RuntimeError("yahoo down")
    f = insider_check("ACME.NS", 30, router({}), TODAY, yf_loader=boom)
    assert f.status == "unknown" and f.desk == "insider"
    assert insider_check("ACME.NS", 30, lambda u: b"not json", TODAY, yf_loader=boom).status == "unknown"


def test_helpers():
    assert rupees(7.2e7) == "Rs 7.2 cr" and rupees(9.6e5) == "Rs 9.6 lakh" and rupees(500) == "Rs 500"
    rows = parse_pit(json.loads((FIX / "pit_acme_pledge.json").read_text()))
    assert rows[0]["action"] == "pledge" and summarise_trades(rows)["pledges"] == 1
