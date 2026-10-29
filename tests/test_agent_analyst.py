from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from papertrader.agent.analyst import analyst_check, earnings_summary, money, quarterly_facts

FIX = Path(__file__).parent / "fixtures" / "agent" / "analyst"


def csv_loader(name: str, currency: str = "INR"):
    """Real yfinance quarterly_income_stmt samples saved as CSV (rows = line items)."""
    def load(_ticker):
        df = pd.read_csv(FIX / name, index_col=0)
        df.columns = pd.to_datetime(df.columns)
        df.attrs["currency"] = currency
        return df
    return load


def frame(rows: dict[str, list], dates: list[str], currency: str = "INR"):
    df = pd.DataFrame(rows, index=pd.to_datetime(dates)).T
    df.attrs["currency"] = currency
    return lambda _t: df


def test_quarterly_facts_real_reliance_sample():
    df = quarterly_facts("RELIANCE.NS", csv_loader("reliance_quarterly.csv"))
    assert list(df.index.strftime("%Y-%m-%d")) == ["2026-06-30", "2026-03-31", "2025-12-31", "2025-06-30", "2025-03-31"]
    assert df.iloc[0]["revenue"] == 3.09468e12
    assert df.iloc[0]["operating_margin"] == round(100 * 324.17 / 3094.68, 1)
    assert df.attrs["currency"] == "INR"


def test_yoy_matches_same_quarter_and_skips_missing_quarter():
    s = earnings_summary("RELIANCE.NS", csv_loader("reliance_quarterly.csv"))
    assert s["year_ago"]["quarter_end"] == "2025-06-30"
    assert s["yoy"]["revenue"] == round(100 * (3094.68 - 2436.32) / 2436.32, 1)
    assert s["yoy"]["net_income"] == round(100 * (209.46 - 269.94) / 269.94, 1)
    assert s["prev_quarter"]["quarter_end"] == "2026-03-31"
    assert s["ttm"] == {}                     # 2025-09-30 missing: no four consecutive quarters
    assert s["bullets"][0].startswith("Q1 FY27 (quarter ended 30 Jun 2026): revenue Rs 309,468 cr (+27.0%")
    assert analyst_check("RELIANCE.NS", csv_loader("reliance_quarterly.csv")).status == "info"


def test_infosys_reports_in_usd_and_has_ttm():
    s = earnings_summary("INFY.NS", csv_loader("infy_quarterly.csv", "USD"))
    assert s["currency"] == "USD" and "$5.14B" in s["bullets"][0]
    assert s["year_ago"]["quarter_end"] == "2025-09-30"
    assert s["ttm"]["revenue"] == 5.145e9 + 5.082e9 + 5.04e9 + 5.099e9
    assert s["yoy"]["eps_diluted"] is None    # Yahoo has no EPS for 2025-09-30


def test_warn_when_revenue_and_net_income_both_fall():
    load = frame({"Total Revenue": [90.0, 95, 98, 99, 100], "Net Income": [8.0, 9, 10, 10, 10],
                  "Operating Income": [12.0, 13, 14, 14, 15], "Diluted EPS": [1.0, 1.1, 1.2, 1.2, 1.25]},
                 ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"])
    f = analyst_check("ACME.NS", load)
    assert f.status == "warn"
    assert f.data["yoy"]["revenue"] == -10.0 and f.data["yoy"]["net_income"] == -20.0
    assert f.data["qoq"]["revenue"] == round(100 * (90 - 95) / 95, 1)
    assert f.data["ttm"]["revenue"] == 90 + 95 + 98 + 99
    assert f.data["yoy"]["net_margin_pts"] == round(round(100 * 8 / 90, 1) - 10.0, 1)


def test_only_one_falling_is_info_and_one_offs_flagged():
    load = frame({"Total Revenue": [90.0, 100], "Net Income": [20.0, 10], "Operating Income": [15.0, 14]},
                 ["2026-09-30", "2025-09-30"])
    f = analyst_check("ACME.NS", load)
    assert f.status == "info"
    assert any("above operating income" in b for b in f.data["bullets"])


def test_no_year_ago_quarter_is_said_plainly():
    load = frame({"Total Revenue": [90.0, 95], "Net Income": [8.0, 9]}, ["2026-09-30", "2026-06-30"])
    f = analyst_check("ACME.NS", load)
    assert f.status == "info" and f.data["year_ago"] is None
    assert any("No same quarter a year earlier" in b for b in f.data["bullets"])


def test_missing_data_is_unknown():
    assert analyst_check("X", lambda t: pd.DataFrame()).status == "unknown"
    assert analyst_check("X", frame({"Total Revenue": [np.nan], "Net Income": [np.nan]}, ["2026-09-30"])).status == "unknown"

    def boom(_t):
        raise ConnectionError("yahoo down")
    f = analyst_check("X", boom)
    assert f.status == "unknown" and "yahoo down" in f.summary


def test_money_format():
    assert money(3.09468e12) == "Rs 309,468 cr"
    assert money(5.5e8) == "Rs 55.0 cr"
    assert money(5.146e9, "USD") == "$5.15B"
