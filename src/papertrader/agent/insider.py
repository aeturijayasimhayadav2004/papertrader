"""Insider desk: the India version of the "Pelosi tracker".

Method (from the US trader-pelosi skill): read the trades insiders are legally
required to disclose, turn each disclosure into a clean row (who, their role,
buy / sell / pledge, quantity, value, date) and say plainly what the insiders did
in the window. It is fast, not early: disclosures lag the trade (SEBI PIT asks
for 2 trading days), so nothing here is inside information, only public filings.

Sources (free, public):
  * SEBI (Prohibition of Insider Trading) Regulations disclosures on NSE, i.e.
    promoter / director / KMP trades and pledges (Reg. 7(2)):
    https://www.nseindia.com/api/corporates-pit?index=equities&symbol=<SYM>&from_date=dd-mm-yyyy&to_date=dd-mm-yyyy
  * NSE bulk and block deals (any party trading > 0.5% of equity, or a block window trade):
    https://www.nseindia.com/api/historical/bulk-deals?symbol=<SYM>&from=dd-mm-yyyy&to=dd-mm-yyyy
    https://www.nseindia.com/api/historical/block-deals?symbol=<SYM>&from=dd-mm-yyyy&to=dd-mm-yyyy
    and, if those fail, today's snapshot https://www.nseindia.com/api/snapshot-capital-market-largedeal
  * Fallback when NSE refuses the script (401/403 from Akamai, common on cloud IPs):
    Yahoo's insider_transactions via yfinance. Yahoo's values for Indian stocks are
    in USD (converted at the USDINR rate from Yahoo, else ~88) and can lag weeks.
Pledges also come from the PIT feed ("Pledge Creation"); the news desk tags
pledge headlines too.

Finding (desk "insider"):
  WARN    net insider selling above NET_SELL_WARN_RS (Rs 5 crore) or a new / invoked pledge;
  INFO    net buying ("promoters bought Rs X"), or selling below the threshold;
  OK      no insider trades in the window;
  UNKNOWN no source answered.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import re
from typing import Any, Callable

import pandas as pd

from .http import Fetch, FetchError, NSESession, get_json, url_with
from .market import now_ist
from .types import INFO, OK, WARN, Finding, symbol, unknown, yahoo

log = logging.getLogger(__name__)

DESK = "insider"
NSE_PIT = "https://www.nseindia.com/api/corporates-pit"
NSE_BULK = "https://www.nseindia.com/api/historical/bulk-deals"
NSE_BLOCK = "https://www.nseindia.com/api/historical/block-deals"
NSE_LARGE_DEALS = "https://www.nseindia.com/api/snapshot-capital-market-largedeal"

NET_SELL_WARN_RS = 5e7          # Rs 5 crore
USD_INR_FALLBACK = 88.0

# Not a market decision by the insider: ESOP allotments, inter-se promoter transfers, gifts...
_NON_MARKET = re.compile(r"esop|allot|inter[- ]?se|gift|transmission|bonus|rights|conversion|scheme|amalgamation", re.I)
_EMPLOYEE = re.compile(r"employee|trust", re.I)

YFLoader = Callable[[str], pd.DataFrame]


def rupees(v: float | None) -> str:
    if v is None:
        return "n/a"
    a = abs(v)
    if a >= 1e7:
        return f"Rs {v / 1e7:,.1f} cr"
    if a >= 1e5:
        return f"Rs {v / 1e5:,.1f} lakh"
    return f"Rs {v:,.0f}"


def _num(v: Any) -> float | None:
    try:
        f = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _date(v: Any) -> dt.date | None:
    if isinstance(v, (dt.datetime, pd.Timestamp)):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%Y %H:%M", "%d-%b-%Y %H:%M:%S", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _ddmmyyyy(d: dt.date) -> str:
    return d.strftime("%d-%m-%Y")


def _action(text: str) -> str:
    t = (text or "").lower()
    if "pledge" in t or "encumb" in t:
        if "invo" in t:
            return "pledge_invoked"
        if "revo" in t or "release" in t:
            return "pledge_released"
        return "pledge"
    if re.search(r"\b(?:buy|purchase|acqui|acquisition)", t):
        return "buy"
    if re.search(r"\b(?:sell|sale|sold|dispos)", t):
        return "sell"
    return "other"


def _rows(doc: Any) -> list[dict]:
    rows = doc.get("data", []) if isinstance(doc, dict) else (doc or [])
    return [r for r in rows if isinstance(r, dict)]


# ---------------------------------------------------------------- parsing

def parse_pit(doc: Any) -> list[dict]:
    """NSE corporates-pit JSON -> trade rows."""
    out = []
    for r in _rows(doc):
        kind = str(r.get("tdpTransactionType") or "").strip()
        mode = str(r.get("acqMode") or "").strip()
        action = _action(kind if kind and kind != "-" else mode)
        d = _date(r.get("acqtoDt") or r.get("acqfromDt") or r.get("intimDt") or r.get("date"))
        if d is None:
            continue
        qty = _num(r.get("secAcq"))
        out.append({"date": d.isoformat(), "person": str(r.get("acqName") or "").strip(),
                    "category": str(r.get("personCategory") or "").strip(), "action": action,
                    "qty": int(qty) if qty is not None else None, "value": _num(r.get("secVal")),
                    "mode": mode, "security": str(r.get("secType") or "").strip(),
                    "disclosed": str(r.get("intimDt") or r.get("date") or "").strip(),
                    "source": "NSE PIT"})
    return out


def parse_yf_insiders(df: pd.DataFrame | None, usd_inr: float = USD_INR_FALLBACK) -> list[dict]:
    """yfinance insider_transactions -> trade rows (Value assumed USD, converted to Rs)."""
    if df is None or len(df) == 0:
        return []
    out = []
    for r in df.to_dict("records"):
        d = _date(r.get("Start Date"))
        if d is None:
            continue
        text = f"{r.get('Transaction') or ''} {r.get('Text') or ''}"
        val = _num(r.get("Value"))
        qty = _num(r.get("Shares"))
        out.append({"date": d.isoformat(), "person": str(r.get("Insider") or "").strip(),
                    "category": str(r.get("Position") or "").strip(), "action": _action(text),
                    "qty": int(qty) if qty is not None else None,
                    "value": val * usd_inr if val is not None else None,
                    "mode": str(r.get("Text") or "").strip(), "security": "Equity Shares",
                    "disclosed": "", "source": "yfinance"})
    return out


def parse_deals(doc: Any, kind: str, sym: str | None = None) -> list[dict]:
    """NSE historical bulk/block deals ({"data": [BD_* rows]}) or snapshot rows -> deal rows."""
    out = []
    for r in _rows(doc):
        s = str(r.get("BD_SYMBOL") or r.get("symbol") or "").strip().upper()
        if sym and s and s != sym:
            continue
        d = _date(r.get("BD_DT_DATE") or r.get("date") or r.get("mTIMESTAMP"))
        qty = _num(r.get("BD_QTY_TRD") or r.get("qty"))
        price = _num(r.get("BD_TP_WATP") or r.get("watp"))
        side = str(r.get("BD_BUY_SELL") or r.get("buySell") or "").strip().upper()
        if d is None:
            continue
        out.append({"date": d.isoformat(), "client": str(r.get("BD_CLIENT_NAME") or r.get("clientName") or "").strip(),
                    "action": "buy" if side.startswith("B") else "sell" if side.startswith("S") else side.lower(),
                    "qty": int(qty) if qty is not None else None, "price": price,
                    "value": qty * price if qty is not None and price is not None else None,
                    "kind": kind, "source": "NSE"})
    return out


# ---------------------------------------------------------------- fetching

def _default_yf_loader(ticker: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.Ticker(yahoo(ticker)).insider_transactions
    df = df.copy() if df is not None else pd.DataFrame()
    rate = None
    try:
        rate = float(yf.Ticker("INR=X").fast_info["lastPrice"])
    except Exception:
        pass
    df.attrs["usd_inr"] = rate if rate and 50 < rate < 200 else USD_INR_FALLBACK
    return df


def _window(rows: list[dict], start: dt.date, end: dt.date) -> list[dict]:
    keep = [r for r in rows if start.isoformat() <= r["date"] <= end.isoformat()]
    return sorted(keep, key=lambda r: r["date"], reverse=True)


def insider_trades(ticker: str, days: int = 30, fetch: Fetch | None = None, today: dt.date | None = None,
                   yf_loader: YFLoader | None = None, notes: list[str] | None = None) -> list[dict]:
    """Insider / promoter trades disclosed in the last `days`, newest first.

    NSE PIT first; Yahoo (yfinance) when NSE fails. Raises FetchError if both fail.
    Each row: date, person, category, action (buy/sell/pledge/pledge_released/
    pledge_invoked/other), qty, value (Rs), mode, security, disclosed, source.
    """
    sym = symbol(ticker)
    end = today or now_ist().date()
    start = end - dt.timedelta(days=days)
    notes = [] if notes is None else notes
    url = url_with(NSE_PIT, index="equities", symbol=sym, from_date=_ddmmyyyy(start), to_date=_ddmmyyyy(end))
    try:
        return _window(parse_pit(get_json(url, fetch or NSESession())), start, end)
    except Exception as exc:
        notes.append(f"NSE PIT unavailable ({str(exc)[:120]}); used Yahoo insider data")
    try:
        df = (yf_loader or _default_yf_loader)(sym)
        rate = (df.attrs.get("usd_inr") if df is not None else None) or USD_INR_FALLBACK
        return _window(parse_yf_insiders(df, rate), start, end)
    except Exception as exc:
        raise FetchError(f"{notes[-1]}; yfinance insiders: {exc}") from exc


def bulk_block_deals(ticker: str, days: int = 30, fetch: Fetch | None = None,
                     today: dt.date | None = None) -> list[dict]:
    """NSE bulk + block deals in the stock over the last `days`, newest first. Raises FetchError if NSE fails."""
    sym = symbol(ticker)
    end = today or now_ist().date()
    start = end - dt.timedelta(days=days)
    nse = fetch or NSESession()
    deals: list[dict] = []
    errors: list[str] = []
    for kind, base in (("bulk", NSE_BULK), ("block", NSE_BLOCK)):
        try:
            deals += parse_deals(get_json(url_with(base, symbol=sym, **{"from": _ddmmyyyy(start), "to": _ddmmyyyy(end)}), nse), kind, sym)
        except Exception as exc:
            errors.append(f"{kind} deals: {exc}")
            if kind == "bulk":
                break   # NSE refused once: do not keep knocking
    if len(errors) and not deals:
        try:
            doc = get_json(NSE_LARGE_DEALS, nse)
            deals += parse_deals((doc or {}).get("BULK_DEALS_DATA", []), "bulk", sym)
            deals += parse_deals((doc or {}).get("BLOCK_DEALS_DATA", []), "block", sym)
        except Exception as exc:
            errors.append(f"large-deal snapshot: {exc}")
            raise FetchError("; ".join(errors)) from exc
    return _window(deals, start, end)


# ---------------------------------------------------------------- the finding

def _is_market(r: dict) -> bool:
    return not _NON_MARKET.search(r.get("mode") or "") and not _EMPLOYEE.search(r.get("category") or "") \
        and not _EMPLOYEE.search(r.get("person") or "")


def summarise_trades(trades: list[dict]) -> dict[str, Any]:
    buy = sum(r["value"] or 0 for r in trades if r["action"] == "buy" and _is_market(r))
    sell = sum(r["value"] or 0 for r in trades if r["action"] == "sell" and _is_market(r))
    promo = [r for r in trades if "promoter" in (r["category"] or "").lower() and _is_market(r)]
    p_buy = sum(r["value"] or 0 for r in promo if r["action"] == "buy")
    p_sell = sum(r["value"] or 0 for r in promo if r["action"] == "sell")
    pledges = [r for r in trades if r["action"] in ("pledge", "pledge_invoked")]
    return {"buy_value": buy, "sell_value": sell, "net_value": buy - sell,
            "promoter_buy_value": p_buy, "promoter_sell_value": p_sell,
            "pledges": len(pledges), "pledge_rows": pledges[:5]}


def insider_check(ticker: str, days: int = 30, fetch: Fetch | None = None, today: dt.date | None = None,
                  yf_loader: YFLoader | None = None, threshold_rs: float = NET_SELL_WARN_RS) -> Finding:
    """WARN on heavy net insider selling or new pledges; never raises."""
    sym = symbol(ticker)
    end = today or now_ist().date()
    nse = fetch or NSESession()
    notes: list[str] = []
    try:
        trades = insider_trades(sym, days, nse, end, yf_loader=yf_loader, notes=notes)
    except Exception as exc:
        return unknown(DESK, str(exc)[:200])
    deals: list[dict] = []
    if notes:   # NSE already refused this run: skip the deal endpoints
        notes.append("bulk/block deals skipped (NSE unavailable)")
    else:
        try:
            deals = bulk_block_deals(sym, days, nse, end)
        except Exception as exc:
            notes.append(f"bulk/block deals unavailable: {str(exc)[:120]}")
    try:
        s = summarise_trades(trades)
        src = trades[0]["source"] if trades else ("yfinance" if notes and "Yahoo" in notes[0] else "NSE PIT")
        data: dict[str, Any] = {"days": days, "source": src, **{k: v for k, v in s.items() if k != "pledge_rows"},
                                "trades": trades[:20], "deals": deals[:20], "notes": notes}
        window = f"last {days} days"
        deal_txt = ""
        if deals:
            d_buy = sum(d["value"] or 0 for d in deals if d["action"] == "buy")
            d_sell = sum(d["value"] or 0 for d in deals if d["action"] == "sell")
            deal_txt = (f" {len(deals)} bulk/block deal{'s' if len(deals) != 1 else ''}: "
                        f"bought {rupees(d_buy)}, sold {rupees(d_sell)}.")
        via = " (Yahoo data, NSE unavailable)" if src == "yfinance" else ""
        if s["pledges"]:
            p = s["pledge_rows"][0]
            verb = "invoked a pledge on" if p["action"] == "pledge_invoked" else "pledged"
            return Finding(DESK, WARN, f"{sym}: {s['pledges']} pledge disclosure(s) in the {window}{via}; "
                                       f"latest {p['person'] or 'an insider'} ({p['category'] or 'insider'}) {verb} "
                                       f"{p['qty'] or '?'} shares on {p['date']}.{deal_txt}", data)
        if not trades:
            return Finding(DESK, OK if not deals else INFO,
                           f"{sym}: no insider trades disclosed in the {window}{via}.{deal_txt}", data)
        net = s["net_value"]
        who = "promoters" if (s["promoter_buy_value"] or s["promoter_sell_value"]) else "insiders"
        if -net > threshold_rs:
            return Finding(DESK, WARN, f"{sym}: {who} net sold {rupees(-net)} in the {window}{via} "
                                       f"(sold {rupees(s['sell_value'])}, bought {rupees(s['buy_value'])}); "
                                       f"above the {rupees(threshold_rs)} alert level.{deal_txt}", data)
        if net > 0:
            return Finding(DESK, INFO, f"{sym}: {who} bought net {rupees(net)} in the {window}{via} "
                                       f"({len(trades)} disclosure{'s' if len(trades) != 1 else ''}).{deal_txt}", data)
        if net < 0:
            return Finding(DESK, INFO, f"{sym}: {who} net sold {rupees(-net)} in the {window}{via}, "
                                       f"below the {rupees(threshold_rs)} alert level.{deal_txt}", data)
        return Finding(DESK, INFO, f"{sym}: {len(trades)} insider disclosure(s) in the {window}{via}, "
                                   f"none an open-market buy or sale (ESOP, transfers, other).{deal_txt}", data)
    except Exception as exc:
        return unknown(DESK, f"bad insider data: {exc}"[:200])
