"""News desk: recent headlines per stock, tagged with what tends to move an Indian stock.

Method (ported from the US trader-news skill): read a headline feed for each stock,
keep only the window asked for, drop duplicates (the same story syndicated by
several papers), sort newest first and tag every headline by keyword: results,
dividend, buyback, bonus/split, order win, deal, fundraise, rating up/downgrade,
target cut, regulatory action, management exit, pledge, fraud/default, guidance.
Headlines are leads, not facts: the desk only flags what to read before a trade.

Sources (free, no keys):
  * Google News RSS, India edition:
    https://news.google.com/rss/search?q=<company> (share OR stock OR NSE) when:2d&hl=en-IN&gl=IN&ceid=IN:en
    The company name comes from a built-in map for the default universe, else
    Yahoo's shortName, else the NSE symbol itself.
  * NSE corporate announcements (what the company itself filed with the exchange):
    https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=<SYM>
    Best effort: NSE's bot protection often answers 401/403 to scripts, and then
    the Google feed alone is used.

Finding (desk "news"):
  BLOCK  a severe negative in the window: fraud / default / insolvency, a
         regulatory action (SEBI / RBI / CCI / ED order, raid, penalty, tax demand),
         or the auditor or CFO resigning;
  WARN   a broker downgrade, target cut or guidance cut;
  INFO   headlines found, nothing alarming;  OK  no headlines at all;
  UNKNOWN every source failed.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import logging
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from .http import Fetch, FetchError, NSESession, default_fetch, get_json, url_with
from .market import IST, now_ist
from .types import BLOCK, INFO, OK, WARN, Finding, symbol, unknown, yahoo

log = logging.getLogger(__name__)

DESK = "news"
GOOGLE_NEWS = "https://news.google.com/rss/search"
NSE_ANNOUNCEMENTS = "https://www.nseindia.com/api/corporate-announcements"

# Names people (and newspapers) actually use for the default universe.
COMPANY_NAMES: dict[str, str] = {
    "RELIANCE": "Reliance Industries",
    "TCS": "TCS",
    "HDFCBANK": "HDFC Bank",
    "ICICIBANK": "ICICI Bank",
    "INFY": "Infosys",
    "HINDUNILVR": "Hindustan Unilever",
    "ITC": "ITC",
    "SBIN": "SBI",
    "BHARTIARTL": "Bharti Airtel",
    "LT": "Larsen & Toubro",
}

_REGULATOR = r"\b(?:SEBI|RBI|CCI|ED|NCLT|NCLAT|SFIO|CBI|DGGI|GST|IRDAI|Enforcement Directorate|income[- ]tax|I-T|tax department)\b"
_ACTION = (r"\b(?:raids?|raided|searche[sd]|search operations?|penalty|penalt(?:y|ies)|fine[sd]?|"
           r"show[- ]cause|notice|bans?|banned|bars?|barred|restrain(?:s|ed)?|probes?|probing|"
           r"investigat(?:es|ion|ing)|order(?:s|ed)? against|tax demand|demand order|summons|"
           r"attach(?:es|ed|ment)|seize[sd]?|crackdown|action against)\b")

# (tag, regex) checked against the headline, case-insensitive.
TAGS: list[tuple[str, str]] = [
    ("results", r"\bresults?\b|\bQ[1-4]\b|\bearnings\b|\bnet (?:profit|loss)\b|\bPAT\b|\bprofit (?:rises|falls|jumps|drops|up|down)"),
    ("dividend", r"\bdividends?\b"),
    ("buyback", r"\bbuy[- ]?backs?\b|\bshare repurchase\b"),
    ("bonus_split", r"\bbonus (?:issue|shares?)\b|\bstock split\b|\bshare split\b|\bsplits? shares\b|\bsub-?division\b"),
    ("order_win", r"\b(?:bags?|wins?|won|secures?|secured|bagged|receives?|bags) .{0,40}\b(?:orders?|contracts?|deal)\b"
                  r"|\border(?:s)? (?:worth|win|inflow)\b|\bletter of (?:award|intent)\b|\bLoA\b"),
    ("deal", r"\bacqui(?:re|res|red|ring|sition)s?\b|\bmergers?\b|\bamalgamation\b|\btakeover\b|\bdemerger\b"
             r"|\bjoint venture\b|\bJV\b|\bstake (?:sale|buy|in)\b|\bbuys? .{0,30}stake\b"),
    ("fundraise", r"\bQIP\b|\brights issue\b|\bblock deals?\b|\bbulk deals?\b|\bOFS\b|\boffer for sale\b"
                  r"|\bfund ?rais(?:e|es|ing)\b|\braises? (?:Rs|₹|INR|\$)|\bNCDs?\b|\bpreferential (?:issue|allotment)\b"),
    ("rating_upgrade", r"\bupgrade[sd]?\b|\braises? (?:\w+ )?target\b|\btarget (?:price )?(?:hiked?|raised)\b|\boverweight\b|\bupgrades? to buy\b"),
    ("rating_downgrade", r"\bdowngrade[sd]?\b|\bunderweight\b|\bcut to (?:sell|reduce|hold|neutral)\b|\b(?:sell|reduce) rating\b"),
    ("target_cut", r"\b(?:cuts?|lowers?|slash(?:es)?|trims?|reduces?|pares?)\b.{0,30}\b(?:target|TP)\b"
                   r"|\btarget (?:price )?(?:cut|lowered|slashed|trimmed)\b"),
    ("guidance", r"\bguidance\b|\boutlook\b|\bforecast\b"),
    ("management_exit", r"\b(?:CEO|CFO|COO|CTO|MD|managing director|chairman|chairperson|auditors?|company secretary|"
                        r"chief \w+ officer|director|whole[- ]time director)\b.{0,40}\b(?:resign(?:s|ed|ation)?|quits?|"
                        r"steps? down|stepped down|exits?|leaves?|left)\b|\bresignation of\b"),
    ("pledge", r"\bpledge[sd]?\b|\bencumbr"),
    ("fraud_default", r"\bfraud\b|\bdefaults?\b|\bdefaulted\b|\binsolvency\b|\bbankrupt|\bforensic audit\b|\bsiphon"
                      r"|\bmoney laundering\b|\bscam\b|\bwilful defaulter\b|\bmisse[sd] (?:interest|coupon|payment)\b"
                      r"|\baccounting (?:irregularit|lapse)"),
]
_COMPILED = [(t, re.compile(rx, re.I)) for t, rx in TAGS]
_REG_RX = re.compile(_REGULATOR, re.I)
_ACT_RX = re.compile(_ACTION, re.I)
_STANDALONE_ACTION = re.compile(r"\b(?:I-T|income[- ]tax|tax) raids?\b|\braid(?:s|ed)? (?:on|at)\b|\bshow[- ]cause notice\b"
                                r"|\bimposes? (?:a )?(?:penalty|fine)\b|\bpenalty of\b", re.I)
_GUIDANCE_CUT = re.compile(r"\b(?:cuts?|lowers?|lowered|slash(?:es|ed)?|trims?|trimmed|reduces?|reduced|weaker|muted)\b"
                           r".{0,40}\b(?:guidance|outlook|forecast)\b|\b(?:guidance|outlook|forecast)\b.{0,15}\b(?:cut|lowered|trimmed|miss)\b", re.I)
# Regulatory actions serious enough to skip a buy; fines, notices, tax demands and probes only warn
# (large companies get those every week).
_REG_SEVERE = re.compile(r"\b(?:raids?|raided|search operations?|bans?|banned|bars?|barred|restrain(?:s|ed)?|"
                         r"attach(?:es|ed|ment)|seize[sd]?|freez(?:e|es)|frozen|suspend(?:s|ed)?|"
                         r"licen[cs]e cancell?ed)\b", re.I)
_AUDITOR_CFO = re.compile(r"\b(?:auditors?|CFO|chief financial officer)\b", re.I)

SEVERE_TAGS = ("fraud_default", "regulatory_severe")
CAUTION_TAGS = ("rating_downgrade", "target_cut", "guidance_cut", "regulatory")


@dataclass
class NewsItem:
    title: str
    source: str
    published: dt.datetime          # aware, IST
    url: str
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "source": self.source, "published": self.published.isoformat(),
                "url": self.url, "tags": list(self.tags)}


# ---------------------------------------------------------------- tagging

def tag_headline(title: str) -> list[str]:
    """Keyword tags for one headline (see TAGS), in a stable order."""
    text = html.unescape(title or "")
    tags = [t for t, rx in _COMPILED if rx.search(text)]
    if (_REG_RX.search(text) and _ACT_RX.search(text)) or _STANDALONE_ACTION.search(text):
        tags.append("regulatory")
        if _REG_SEVERE.search(text):
            tags.append("regulatory_severe")
    if _GUIDANCE_CUT.search(text):
        tags.append("guidance_cut")
    if "management_exit" in tags and _AUDITOR_CFO.search(text):
        tags.append("auditor_cfo_exit")
    return tags


def severity(item: NewsItem) -> str:
    """BLOCK / WARN / INFO for one headline."""
    if any(t in item.tags for t in SEVERE_TAGS) or "auditor_cfo_exit" in item.tags:
        return BLOCK
    if any(t in item.tags for t in CAUTION_TAGS):
        return WARN
    return INFO


# ---------------------------------------------------------------- sources

def company_name(ticker: str) -> str:
    """Name to search for: built-in map, else Yahoo's shortName (cleaned), else the symbol."""
    sym = symbol(ticker)
    if sym in COMPANY_NAMES:
        return COMPANY_NAMES[sym]
    try:
        import yfinance as yf
        name = (yf.Ticker(yahoo(sym)).info or {}).get("shortName") or ""
        name = re.sub(r"\b(?:LTD|LIMITED|LTD\.|INDUSTRIES LTD)\b\.?", "", name, flags=re.I).strip(" .,")
        if name:
            return name.title() if name.isupper() else name
    except Exception as exc:  # network or yfinance trouble: the symbol still works as a query
        log.debug("shortName for %s unavailable: %s", sym, exc)
    return sym


def google_news_url(name: str, hours: int) -> str:
    days = max(1, math.ceil(hours / 24))
    q = f'"{name}" (share OR stock OR NSE) when:{days}d'
    return url_with(GOOGLE_NEWS, q=q, hl="en-IN", gl="IN", ceid="IN:en")


def _parse_rfc822(value: str) -> dt.datetime | None:
    try:
        d = email.utils.parsedate_to_datetime((value or "").strip())
    except (TypeError, ValueError, IndexError):
        return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(IST)


def parse_google_rss(data: bytes) -> list[NewsItem]:
    """Google News RSS -> items. Titles end in ' - Publisher'; that suffix becomes the source."""
    root = ET.fromstring(data)
    out: list[NewsItem] = []
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        source = (it.findtext("source") or "").strip()
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].rstrip()
        elif not source and " - " in title:
            title, source = title.rsplit(" - ", 1)
        when = _parse_rfc822(it.findtext("pubDate") or "")
        if not title or when is None:
            continue
        out.append(NewsItem(title, source or "Google News", when, (it.findtext("link") or "").strip(),
                            tag_headline(title)))
    return out


_NSE_TIME_FORMATS = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S", "%d-%b-%Y")


def parse_nse_time(value: str) -> dt.datetime | None:
    value = (value or "").strip()
    for fmt in _NSE_TIME_FORMATS:
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def parse_nse_announcements(doc: Any) -> list[NewsItem]:
    """NSE corporate-announcements JSON (a list, or {"data": [...]}) -> items."""
    rows = doc.get("data", []) if isinstance(doc, dict) else (doc or [])
    out: list[NewsItem] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        desc = str(r.get("desc") or "").strip()
        text = re.sub(r"\s+", " ", str(r.get("attchmntText") or "")).strip()
        title = desc if not text else f"{desc}: {text[:180]}{'...' if len(text) > 180 else ''}"
        when = parse_nse_time(r.get("an_dt") or r.get("sort_date") or r.get("exchdisstime") or r.get("dt") or "")
        if not title or when is None:
            continue
        out.append(NewsItem(title, "NSE filing", when, str(r.get("attchmntFile") or ""),
                            tag_headline(f"{desc}: {text}")))
    return out


def _norm(title: str) -> str:
    return re.sub(r"\W+", " ", title.lower()).strip()[:80]


def dedupe(items: list[NewsItem]) -> list[NewsItem]:
    """One item per story (same link or same normalised title), newest first."""
    seen: set[str] = set()
    out: list[NewsItem] = []
    for it in sorted(items, key=lambda i: i.published, reverse=True):
        keys = {k for k in (it.url, _norm(it.title)) if k}
        if keys & seen:
            continue
        seen |= keys
        out.append(it)
    return out


def fetch_news(ticker: str, hours: int = 24, company: str | None = None, fetch: Fetch | None = None,
               now: dt.datetime | None = None) -> list[NewsItem]:
    """Headlines on `ticker` from the last `hours`, newest first.

    Raises FetchError only when every source failed; an NSE failure alone is logged.
    `fetch` (url -> bytes), when given, is used for every source (tests).
    """
    sym = symbol(ticker)
    t_now = now_ist(now)
    cutoff = t_now - dt.timedelta(hours=hours)
    ahead = t_now + dt.timedelta(minutes=10)
    name = company or company_name(sym)
    items: list[NewsItem] = []
    errors: list[str] = []
    try:
        items += parse_google_rss((fetch or default_fetch)(google_news_url(name, hours)))
    except Exception as exc:
        errors.append(f"Google News: {exc}")
    try:
        doc = get_json(url_with(NSE_ANNOUNCEMENTS, index="equities", symbol=sym), fetch or NSESession())
        items += parse_nse_announcements(doc)
    except Exception as exc:
        errors.append(f"NSE announcements: {exc}")
    if len(errors) == 2:
        raise FetchError("; ".join(errors))
    for e in errors:
        log.info("news %s: %s", sym, e)
    return dedupe([i for i in items if cutoff <= i.published <= ahead])


# ---------------------------------------------------------------- the finding

def _line(it: NewsItem) -> str:
    tags = f" [{', '.join(it.tags)}]" if it.tags else ""
    return f'"{it.title}" ({it.source}, {it.published:%d %b %H:%M} IST){tags}'


def news_finding(ticker: str, items: list[NewsItem]) -> Finding:
    sym = symbol(ticker)
    data = {"count": len(items), "items": [i.to_dict() for i in items[:15]]}
    if not items:
        return Finding(DESK, OK, f"{sym}: no headlines in the window.", data)
    severe = [i for i in items if severity(i) == BLOCK]
    caution = [i for i in items if severity(i) == WARN]
    if severe:
        status, lead = BLOCK, severe
        what = "severe negative news"
    elif caution:
        status, lead = WARN, caution
        what = "downgrade / target or guidance cut"
    else:
        status, lead, what = INFO, items, "headlines"
    top = lead[:2] + [i for i in items if i not in lead[:2]][: max(0, 3 - len(lead[:2]))]
    summary = (f"{sym}: {len(items)} headline{'s' if len(items) != 1 else ''}"
               f"{'' if status == INFO else f'; {what}'}. " + "; ".join(_line(i) for i in top[:3]) + ".")
    data["flagged"] = [i.to_dict() for i in lead[:5]] if status != INFO else []
    return Finding(DESK, status, summary, data)


def news_check(ticker: str, hours: int = 48, fetch: Fetch | None = None,
               now: dt.datetime | None = None, company: str | None = None) -> Finding:
    """fetch_news + news_finding; never raises (status "unknown" when no source answered)."""
    try:
        return news_finding(ticker, fetch_news(ticker, hours=hours, company=company, fetch=fetch, now=now))
    except Exception as exc:
        return unknown(DESK, str(exc)[:200])
