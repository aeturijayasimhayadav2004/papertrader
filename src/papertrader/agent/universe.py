"""Index universes: the Nifty 50 / 100 / 500 constituents as Yahoo tickers.

NSE's index arm (niftyindices.com) publishes one CSV per index with columns
"Company Name, Industry, Symbol, Series, ISIN Code". We keep the Symbol column,
turn it into a Yahoo ticker ("INFY" -> "INFY.NS") and cache the raw CSV:

  * a cache younger than `max_age_days` is used without a download;
  * otherwise we download; if that fails, the stale cache is used (constituents
    change twice a year, so a month-old list is still fine);
  * only when there is neither a download nor a cache do we raise UniverseError.

The "config" universe (the tickers in config.yaml) is handled by the caller.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
from pathlib import Path

from papertrader.agent.http import Fetch, default_fetch
from papertrader.agent.types import yahoo
from papertrader.config import PROJECT_ROOT

log = logging.getLogger(__name__)

BASE_URL = "https://niftyindices.com/IndexConstituent/"
INDEX_FILES = {
    "nifty50": "ind_nifty50list.csv",
    "nifty100": "ind_nifty100list.csv",
    "nifty500": "ind_nifty500list.csv",
}
DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "universe"


class UniverseError(RuntimeError):
    pass


def index_url(name: str) -> str:
    return BASE_URL + INDEX_FILES[name]


def parse_constituents(text: str) -> list[str]:
    """CSV text -> sorted unique Yahoo tickers.

    Every listed series is kept: EQ (ordinary shares), BE (trade-for-trade) and
    RR (REITs, which the Nifty 500 now holds) all trade under SYMBOL.NS on Yahoo.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        return []
    cols = {c.strip().lower(): c for c in reader.fieldnames}
    sym_col = cols.get("symbol")
    if sym_col is None:
        return []
    out: set[str] = set()
    for row in reader:
        sym = (row.get(sym_col) or "").strip()
        if sym and " " not in sym:
            out.add(yahoo(sym))
    return sorted(out)


def load_universe(name: str = "nifty50", fetch: Fetch | None = None, cache_dir: Path | None = None,
                  max_age_days: int = 7) -> list[str]:
    """Yahoo tickers of an index, e.g. ["ADANIENT.NS", ..., "WIPRO.NS"]."""
    key = name.strip().lower().replace(" ", "").replace("_", "")
    if key not in INDEX_FILES:
        raise UniverseError(f"unknown universe {name!r}; use one of {', '.join(INDEX_FILES)} (or 'config')")
    cache = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    path = cache / f"{key}.csv"

    cached: list[str] = []
    age: dt.timedelta | None = None
    if path.exists():
        try:
            cached = parse_constituents(path.read_text(encoding="utf-8"))
            age = dt.datetime.now() - dt.datetime.fromtimestamp(path.stat().st_mtime)
        except (OSError, ValueError) as exc:
            log.warning("Universe cache %s unreadable: %s", path, exc)
    if cached and age is not None and age <= dt.timedelta(days=max_age_days):
        return cached

    try:
        text = (fetch or default_fetch)(index_url(key)).decode("utf-8", "replace")
        tickers = parse_constituents(text)
        if not tickers:
            raise UniverseError("the constituent file had no symbols")
        try:
            cache.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:  # a read-only disk should not cost us the list
            log.warning("Could not cache universe %s: %s", key, exc)
        return tickers
    except Exception as exc:  # network trouble, a changed page, an empty file
        if cached:
            days = age.days if age is not None else "?"
            log.warning("Universe %s download failed (%s); using cached list (%s days old)", key, exc, days)
            return cached
        raise UniverseError(f"could not load the {key} universe: download failed ({exc}) and no cache "
                            f"at {path}") from exc
