"""Daily OHLCV download from Yahoo Finance with an on-disk CSV cache.

Prices are split/dividend adjusted (auto_adjust=True) so signals and fills use
one consistent price series.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
import warnings
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from papertrader.config import DataConfig

log = logging.getLogger(__name__)

COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
PRICE_COLUMNS = ["Open", "High", "Low", "Close"]

Downloader = Callable[[str, dt.date], pd.DataFrame]


class DataError(RuntimeError):
    pass


def yahoo_download(ticker: str, start: dt.date) -> pd.DataFrame:
    import yfinance as yf

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # yfinance's own pandas deprecation noise
        return yf.Ticker(ticker).history(start=start.isoformat(), interval="1d", auto_adjust=True, actions=False)


def clean(raw: pd.DataFrame | None) -> pd.DataFrame:
    """Normalise a raw download to tz-naive daily OHLCV, sorted, no bad rows."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], name="Date"), dtype=float)
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise DataError(f"download is missing columns {missing}")
    df = df[COLUMNS]
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df.index.name = "Date"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna(subset=PRICE_COLUMNS)
    df = df[(df[PRICE_COLUMNS] > 0).all(axis=1)]
    df["Volume"] = df["Volume"].fillna(0)
    return df.astype(float)


def drop_incomplete_bar(df: pd.DataFrame, now: dt.datetime, cutoff: dt.time) -> pd.DataFrame:
    """Drop today's bar if the market has not closed yet (now must be in market tz)."""
    if df.empty:
        return df
    last = df.index[-1].date()
    if last > now.date() or (last == now.date() and now.time() < cutoff):
        return df.iloc[:-1]
    return df


def fetch_history(
    ticker: str,
    start: dt.date,
    downloader: Downloader = yahoo_download,
    retries: int = 3,
    pause: float = 2.0,
) -> pd.DataFrame:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            df = clean(downloader(ticker, start))
            if df.empty:
                raise DataError(f"no rows returned for {ticker}")
            return df
        except Exception as exc:  # network errors, rate limits, empty data
            last_exc = exc
            log.warning("Download of %s failed (attempt %d/%d): %s", ticker, attempt, retries, exc)
            if attempt < retries:
                time.sleep(pause * attempt)
    raise DataError(f"download failed for {ticker}: {last_exc}") from last_exc


class PriceCache:
    """One CSV per ticker plus a small JSON file recording when it was fetched."""

    def __init__(self, cache_dir: Path, ttl_hours: float, cutoff: dt.time):
        self.cache_dir = Path(cache_dir)
        self.ttl = dt.timedelta(hours=ttl_hours)
        self.cutoff = cutoff

    def _paths(self, ticker: str) -> tuple[Path, Path]:
        safe = ticker.replace("^", "_").replace("/", "_")
        return self.cache_dir / f"{safe}.csv", self.cache_dir / f"{safe}.meta.json"

    def read(self, ticker: str) -> pd.DataFrame | None:
        csv, _ = self._paths(ticker)
        if not csv.exists():
            return None
        df = pd.read_csv(csv, index_col="Date", parse_dates=["Date"])
        return clean(df)

    def fetched_at(self, ticker: str) -> dt.datetime | None:
        _, meta = self._paths(ticker)
        if not meta.exists():
            return None
        try:
            return dt.datetime.fromisoformat(json.loads(meta.read_text())["fetched_at"])
        except (ValueError, KeyError, json.JSONDecodeError):
            return None

    def is_fresh(self, ticker: str, now: dt.datetime) -> bool:
        fetched = self.fetched_at(ticker)
        if fetched is None or not self._paths(ticker)[0].exists():
            return False
        if now - fetched > self.ttl:
            return False
        # Fetched before today's close but it is now after the close: today's bar is missing.
        close_today = now.replace(hour=self.cutoff.hour, minute=self.cutoff.minute, second=0, microsecond=0)
        return not (fetched < close_today <= now)

    def write(self, ticker: str, df: pd.DataFrame, now: dt.datetime) -> None:
        csv, meta = self._paths(ticker)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(csv)
        meta.write_text(json.dumps({"ticker": ticker, "fetched_at": now.isoformat(), "rows": len(df)}))


def load_prices(
    tickers: list[str],
    cfg: DataConfig,
    refresh: bool = False,
    now: dt.datetime | None = None,
    downloader: Downloader = yahoo_download,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Return ({ticker: OHLCV}, warnings). Only completed daily bars are returned.

    Falls back to stale cache if a download fails; a ticker with no data at all is
    left out and reported in warnings.
    """
    tz = ZoneInfo(cfg.market_tz)
    now = now.astimezone(tz) if now else dt.datetime.now(tz)
    cache = PriceCache(cfg.cache_dir, cfg.cache_ttl_hours, cfg.market_close_cutoff)
    prices: dict[str, pd.DataFrame] = {}
    warnings: list[str] = []

    for ticker in tickers:
        df = None
        if not refresh and cache.is_fresh(ticker, now):
            df = cache.read(ticker)
        if df is None:
            try:
                df = drop_incomplete_bar(fetch_history(ticker, cfg.start, downloader), now, cfg.market_close_cutoff)
                cache.write(ticker, df, now)
            except DataError as exc:
                df = cache.read(ticker)
                if df is None:
                    warnings.append(f"{ticker}: no data available ({exc})")
                    continue
                warnings.append(f"{ticker}: download failed, using cached data up to {df.index[-1].date()}")
        df = drop_incomplete_bar(df, now, cfg.market_close_cutoff)
        if df.empty:
            warnings.append(f"{ticker}: no completed daily bars")
            continue
        prices[ticker] = df

    for w in warnings:
        log.warning(w)
    return prices, warnings
