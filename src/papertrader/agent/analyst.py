"""Analyst desk: what the last quarterly results said, against a year earlier.

Method (ported from the US trader-analyst skill): line the reported quarters up
newest first, compute margins, and compare the latest quarter with the SAME
quarter a year earlier (like with like: Indian businesses are seasonal too,
e.g. festive-quarter consumer demand), then with the quarter before, plus the
trailing twelve months (TTM) when four consecutive quarters are available.
Flag net income above operating income (other income / one-offs inside it).

Source: Yahoo Finance via yfinance, `Ticker('<SYM>.NS').quarterly_income_stmt`
(falls back to `quarterly_financials`). For Indian stocks Yahoo often has only
~5 quarters and gaps (a missing quarter shows as NaN); every comparison is
skipped, not guessed, when its quarter is missing. Some companies report in USD
(Infosys files IFRS in USD), so amounts carry the reporting currency from
Yahoo's `financialCurrency`. Exchange filings (NSE / BSE results PDFs) are the
primary record: read them before relying on a number here.

Finding (desk "analyst"):
  WARN    revenue and net income both fell year on year;
  INFO    otherwise (with the numbers);
  UNKNOWN no usable quarterly data.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable

import pandas as pd

from .types import INFO, WARN, Finding, symbol, unknown, yahoo

log = logging.getLogger(__name__)

DESK = "analyst"
Loader = Callable[[str], pd.DataFrame]

# Yahoo line items, first match wins.
ROWS: dict[str, tuple[str, ...]] = {
    "revenue": ("Total Revenue", "Operating Revenue", "Revenue"),
    "gross_profit": ("Gross Profit",),
    "cost_of_revenue": ("Cost Of Revenue", "Reconciled Cost Of Revenue"),
    "operating_income": ("Operating Income", "Total Operating Income As Reported", "EBIT"),
    "net_income": ("Net Income", "Net Income Common Stockholders", "Net Income Continuous Operations",
                   "Net Income From Continuing Operation Net Minority Interest"),
    "eps_diluted": ("Diluted EPS", "Basic EPS"),
}
COLUMNS = ["revenue", "gross_profit", "operating_income", "net_income", "eps_diluted",
           "gross_margin", "operating_margin", "net_margin"]


def _default_loader(ticker: str) -> pd.DataFrame:
    import yfinance as yf
    t = yf.Ticker(yahoo(ticker))
    df = t.quarterly_income_stmt
    if df is None or df.empty:
        df = t.quarterly_financials
    df = df.copy() if df is not None else pd.DataFrame()
    try:
        df.attrs["currency"] = (t.info or {}).get("financialCurrency") or "INR"
    except Exception:
        df.attrs["currency"] = "INR"
    return df


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def _margin(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def quarterly_facts(ticker: str, loader: Loader | None = None) -> pd.DataFrame:
    """One row per quarter (index = quarter end, newest first). Missing values are NaN.

    df.attrs["currency"] carries the reporting currency (default INR).
    """
    raw = (loader or _default_loader)(ticker)
    currency = (raw.attrs.get("currency") if raw is not None else None) or "INR"
    if raw is None or raw.empty:
        out = pd.DataFrame(columns=COLUMNS)
        out.attrs["currency"] = currency
        return out
    index = {str(i).strip(): i for i in raw.index}

    def line(key: str) -> pd.Series:
        for name in ROWS[key]:
            if name in index:
                return pd.to_numeric(raw.loc[index[name]], errors="coerce")
        return pd.Series(float("nan"), index=raw.columns, dtype=float)

    rev, ni = line("revenue"), line("net_income")
    gp = line("gross_profit")
    cogs = line("cost_of_revenue")
    gp = gp.where(gp.notna(), rev - cogs)
    df = pd.DataFrame({"revenue": rev, "gross_profit": gp, "operating_income": line("operating_income"),
                       "net_income": ni, "eps_diluted": line("eps_diluted")})
    df.index = pd.to_datetime(df.index).normalize()
    df.index.name = "quarter_end"
    df = df[df["revenue"].notna() | df["net_income"].notna()].sort_index(ascending=False)
    df["gross_margin"] = (100 * df["gross_profit"] / df["revenue"]).round(1)
    df["operating_margin"] = (100 * df["operating_income"] / df["revenue"]).round(1)
    df["net_margin"] = (100 * df["net_income"] / df["revenue"]).round(1)
    df = df[COLUMNS].astype(float)
    df.attrs["currency"] = currency
    return df


def money(v: float | None, currency: str = "INR") -> str:
    """Rs in crore (1 cr = 10 million) for INR; billions / millions otherwise."""
    if v is None:
        return "n/a"
    if currency == "INR":
        return f"Rs {v / 1e7:,.0f} cr" if abs(v) >= 1e9 else f"Rs {v / 1e7:,.1f} cr"
    sign = "$" if currency == "USD" else f"{currency} "
    return f"{sign}{v / 1e9:,.2f}B" if abs(v) >= 1e9 else f"{sign}{v / 1e6:,.1f}M"


def _eps(v: float | None, currency: str) -> str:
    if v is None:
        return "n/a"
    return f"Rs {v:,.2f}" if currency == "INR" else f"{'$' if currency == 'USD' else currency + ' '}{v:,.2f}"


def _row(df: pd.DataFrame, ts: pd.Timestamp | None) -> dict[str, Any] | None:
    if ts is None:
        return None
    r = df.loc[ts]
    return {"quarter_end": ts.date().isoformat(), **{c: _num(r[c]) for c in COLUMNS}}


def _match(df: pd.DataFrame, end: pd.Timestamp, days: int, slack: int) -> pd.Timestamp | None:
    best = None
    for ts in df.index:
        gap = abs((ts - (end - pd.Timedelta(days=days))).days)
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, ts)
    return best[1] if best else None


def _quarter_label(ts: pd.Timestamp) -> str:
    """Indian fiscal quarter (April-March): Sep 2026 -> Q2 FY27."""
    q = (ts.month - 4) % 12 // 3 + 1
    fy = ts.year + 1 if ts.month >= 4 else ts.year
    return f"Q{q} FY{fy % 100:02d}"


def earnings_summary(ticker: str, loader: Loader | None = None) -> dict[str, Any]:
    """Latest quarter vs a year earlier and vs the quarter before, TTM, and plain bullets.

    Raises ValueError when there is no usable quarter.
    """
    df = quarterly_facts(ticker, loader)
    cur_code = df.attrs.get("currency", "INR")
    if df.empty:
        raise ValueError("no quarterly results in Yahoo data")
    end = df.index[0]
    year_ago = _match(df, end, 365, 12)
    prev = _match(df, end, 91, 12)
    latest, ya, pq = _row(df, end), _row(df, year_ago), _row(df, prev)

    def changes(base: dict | None) -> dict[str, float | None]:
        if base is None:
            return {}
        out: dict[str, float | None] = {k: _pct(latest[k], base[k]) for k in ("revenue", "net_income", "eps_diluted")}
        for m in ("gross_margin", "operating_margin", "net_margin"):
            out[m + "_pts"] = (round(latest[m] - base[m], 1)
                               if latest[m] is not None and base[m] is not None else None)
        return out

    yoy, qoq = changes(ya), changes(pq)
    ttm: dict[str, float] = {}
    last4 = list(df.index[:4])
    consecutive = len(last4) == 4 and all(70 <= (a - b).days <= 110 for a, b in zip(last4, last4[1:]))
    if consecutive:
        for k in ("revenue", "net_income", "eps_diluted"):
            vals = [_num(df.loc[t, k]) for t in last4]
            if all(v is not None for v in vals):
                ttm[k] = round(sum(vals), 2) if k == "eps_diluted" else sum(vals)

    lab = f"{_quarter_label(end)} (quarter ended {end:%d %b %Y})"
    bullets: list[str] = []
    s = f"{lab}: revenue {money(latest['revenue'], cur_code)}"
    if yoy.get("revenue") is not None:
        s += f" ({yoy['revenue']:+.1f}% vs a year earlier)"
    s += f", net income {money(latest['net_income'], cur_code)}"
    if yoy.get("net_income") is not None:
        s += f" ({yoy['net_income']:+.1f}%)"
    if latest["eps_diluted"] is not None:
        s += f", EPS {_eps(latest['eps_diluted'], cur_code)}"
        if yoy.get("eps_diluted") is not None:
            s += f" ({yoy['eps_diluted']:+.1f}%)"
    bullets.append(s + ".")
    if ya is None:
        bullets.append("No same quarter a year earlier in Yahoo's data, so no year-on-year comparison.")
    for m, name in (("operating_margin", "Operating margin"), ("net_margin", "Net margin")):
        if latest[m] is None:
            continue
        line = f"{name} {latest[m]:.1f}%"
        if yoy.get(m + "_pts") is not None:
            line += f" ({yoy[m + '_pts']:+.1f} points vs a year earlier)"
        bullets.append(line + ".")
    if pq is not None and qoq.get("revenue") is not None:
        q = f"Revenue {qoq['revenue']:+.1f}% vs the quarter before"
        if qoq.get("net_income") is not None:
            q += f", net income {qoq['net_income']:+.1f}%"
        bullets.append(q + " (seasonal: read the year-on-year line first).")
    if (latest["net_income"] is not None and latest["operating_income"] is not None
            and latest["net_income"] > latest["operating_income"]):
        bullets.append("Net income is above operating income: other income or one-offs are inside it.")
    if ttm.get("revenue") is not None:
        t = f"Last four quarters: revenue {money(ttm['revenue'], cur_code)}"
        if ttm.get("net_income") is not None:
            t += f", net income {money(ttm['net_income'], cur_code)}"
        if ttm.get("eps_diluted") is not None:
            t += f", EPS {_eps(ttm['eps_diluted'], cur_code)}"
        bullets.append(t + ".")
    if cur_code != "INR":
        bullets.append(f"Yahoo reports this company's numbers in {cur_code}.")
    return {"ticker": symbol(ticker), "currency": cur_code, "quarters": len(df),
            "latest": latest, "year_ago": ya, "prev_quarter": pq, "yoy": yoy, "qoq": qoq,
            "ttm": ttm, "bullets": bullets}


def analyst_check(ticker: str, loader: Loader | None = None) -> Finding:
    """WARN when revenue and net income both fell year on year; never raises."""
    sym = symbol(ticker)
    try:
        s = earnings_summary(ticker, loader)
    except Exception as exc:
        return unknown(DESK, str(exc)[:200])
    latest = s["latest"]
    if latest["revenue"] is None and latest["net_income"] is None:
        return unknown(DESK, "latest quarter has no revenue or net income")
    rev, ni = s["yoy"].get("revenue"), s["yoy"].get("net_income")
    data = {k: s[k] for k in ("currency", "quarters", "latest", "year_ago", "prev_quarter", "yoy", "qoq", "ttm", "bullets")}
    head = s["bullets"][0]
    if rev is not None and ni is not None and rev < 0 and ni < 0:
        return Finding(DESK, WARN, f"{sym}: revenue and net income both fell year on year. {head}", data)
    return Finding(DESK, INFO, f"{sym}: {head}", data)
