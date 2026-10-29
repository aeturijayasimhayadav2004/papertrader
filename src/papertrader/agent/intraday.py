"""Intraday OHLCV bars (5-minute or 1-minute) from Yahoo, with a short cache.

Yahoo labels each bar by its START time in IST: the 09:15 5-minute bar covers
09:15:00-09:19:59 and is complete at 09:20. We keep only regular-session bars
(09:15 <= start < 15:30) that sit on the bar grid; Yahoo's live tick (a row
stamped 10:37:42) and pre-open / closing-session prints are dropped.

Yahoo's free limits: 5-minute bars go back about 60 days, 1-minute bars about
7 days, so `days` is clamped to those. The bars are unofficial and can lag a
minute or two; a bar still forming at the time of the call may be in the frame,
and callers that need closed bars only (entry.py, scanner.scan_orb) filter by
start + interval <= now themselves.

Cache: one CSV per ticker and interval. While the market is open it is fresh
for CACHE_TTL_OPEN (5 minutes); when it is closed it stays fresh as long as it
was fetched after the latest session's close. A failed download falls back to
the stale cache, then to an empty frame. Nothing here raises in normal use.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import warnings
from pathlib import Path
from typing import Callable

import pandas as pd

from papertrader.agent import market
from papertrader.agent.market import CLOSE, IST, OPEN
from papertrader.config import PROJECT_ROOT

log = logging.getLogger(__name__)

COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
MAX_DAYS = {"1m": 7, "2m": 59, "5m": 59, "15m": 59, "30m": 59}
CACHE_TTL_OPEN = dt.timedelta(minutes=5)
DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "intraday"

IntradayDownloader = Callable[[str, str, int], pd.DataFrame]   # (ticker, interval, days) -> raw frame


def yahoo_intraday(ticker: str, interval: str, days: int) -> pd.DataFrame:
    import yfinance as yf

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # auto_adjust=False: intraday plans need the prices that actually traded.
        return yf.Ticker(ticker).history(period=f"{days}d", interval=interval, auto_adjust=False,
                                         actions=False, prepost=False)


def interval_minutes(interval: str) -> int:
    try:
        return int(interval.rstrip("m"))
    except ValueError:
        return 5


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz=IST, name="Datetime"), dtype=float)


def clean_intraday(raw: pd.DataFrame | None, interval: str = "5m") -> pd.DataFrame:
    """Raw download -> IST-indexed regular-session OHLCV on the bar grid."""
    if raw is None or len(raw) == 0:
        return empty_frame()
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if any(c not in df.columns for c in COLUMNS):
        return empty_frame()
    df = df[COLUMNS]
    idx = pd.DatetimeIndex(pd.to_datetime(df.index))
    # Naive timestamps are taken as IST (that is what Yahoo sends for .NS once localised).
    idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
    df.index = idx
    df.index.name = "Datetime"
    step = interval_minutes(interval)
    clock = df.index.hour * 60 + df.index.minute
    on_grid = (df.index.second == 0) & (((clock - (OPEN.hour * 60 + OPEN.minute)) % step) == 0)
    in_session = (clock >= OPEN.hour * 60 + OPEN.minute) & (clock < CLOSE.hour * 60 + CLOSE.minute)
    df = df[on_grid & in_session]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.apply(pd.to_numeric, errors="coerce")
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    df = df[(df[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
    df["Volume"] = df["Volume"].fillna(0)
    return df.astype(float)


def _paths(cache_dir: Path, ticker: str, interval: str) -> tuple[Path, Path]:
    safe = ticker.replace("^", "_").replace("/", "_").replace("&", "_")
    return cache_dir / f"{safe}_{interval}.csv", cache_dir / f"{safe}_{interval}.meta.json"


def _read_cache(csv: Path, interval: str) -> pd.DataFrame | None:
    if not csv.exists():
        return None
    try:
        df = pd.read_csv(csv, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
        return clean_intraday(df, interval)
    except Exception as exc:  # a half-written or foreign file
        log.warning("Intraday cache %s unreadable: %s", csv, exc)
        return None


def _is_fresh(meta: Path, days: int, now: dt.datetime) -> bool:
    try:
        info = json.loads(meta.read_text())
        fetched = dt.datetime.fromisoformat(info["fetched_at"]).astimezone(IST)
        if int(info.get("days", 0)) < days:
            return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    status = market.market_status(now)
    if status.is_open:
        return now - fetched < CACHE_TTL_OPEN
    # Closed: fresh if fetched after the most recent session closed.
    last_day = now.date() if (market.is_trading_day(now.date()) and now.time() >= CLOSE) \
        else market.previous_trading_day(now.date())
    return fetched >= dt.datetime.combine(last_day, CLOSE, IST) and fetched <= now


def load_intraday(ticker: str, interval: str = "5m", days: int = 59, downloader: IntradayDownloader | None = None,
                  cache_dir: Path | None = None, now: dt.datetime | None = None) -> pd.DataFrame:
    """OHLCV bars for `ticker`, tz-aware IST index of bar START times, regular session only.

    Bars starting after `now` are dropped (so a replay clock never sees the future).
    Returns an empty frame when there is no data at all.
    """
    now = market.now_ist(now)
    days = max(1, min(int(days), MAX_DAYS.get(interval, 59)))
    cache = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    csv, meta = _paths(cache, ticker, interval)

    df = _read_cache(csv, interval) if _is_fresh(meta, days, now) else None
    if df is None or df.empty:
        try:
            df = clean_intraday((downloader or yahoo_intraday)(ticker, interval, days), interval)
            if df.empty:
                raise ValueError("no intraday rows returned")
            try:
                cache.mkdir(parents=True, exist_ok=True)
                df.to_csv(csv)
                meta.write_text(json.dumps({"ticker": ticker, "interval": interval, "days": days,
                                            "fetched_at": now.isoformat(), "rows": len(df)}))
            except OSError as exc:
                log.warning("Could not cache intraday %s: %s", ticker, exc)
        except Exception as exc:  # network, rate limit, delisted ticker
            stale = _read_cache(csv, interval)
            if stale is None or stale.empty:
                log.warning("Intraday %s %s unavailable: %s", ticker, interval, exc)
                return empty_frame()
            log.warning("Intraday %s download failed (%s); using cached bars to %s", ticker, exc,
                        stale.index[-1].strftime("%d %b %H:%M"))
            df = stale
    return df[df.index <= now]


def session_days(bars: pd.DataFrame) -> list[dt.date]:
    """Distinct IST dates that have bars, oldest first."""
    if bars is None or bars.empty:
        return []
    idx = pd.DatetimeIndex(bars.index)
    idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
    return sorted(set(idx.date))


def bars_on(bars: pd.DataFrame, day: dt.date) -> pd.DataFrame:
    """The bars of one IST date (empty frame if none)."""
    if bars is None or bars.empty:
        return empty_frame()
    idx = pd.DatetimeIndex(bars.index)
    idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
    out = bars.copy()
    out.index = idx
    return out[idx.date == day]
