"""The daily chart scan the runner does after the close: which stocks in the wider
universe (Nifty 50 by default) show a setup today. Information only: the scan
never places an order; it tells you (dashboard, Telegram) where to look.

Only about a year of daily bars is needed, kept in its own cache folder so the
bot's long backtest history is not re-downloaded for 50 or 500 stocks.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging

import pandas as pd

from papertrader.config import Config
from papertrader.data import load_prices

log = logging.getLogger(__name__)


def scan_prices(cfg: Config, universe_prices: dict[str, pd.DataFrame], now: dt.datetime | None = None
                ) -> dict[str, pd.DataFrame]:
    ag = cfg.agent
    if ag.scan_universe == "config":
        return dict(universe_prices)
    from papertrader.agent.universe import load_universe

    tickers = load_universe(ag.scan_universe, cache_dir=cfg.data.cache_dir / "universe")
    start = (now or dt.datetime.now()).date() - dt.timedelta(days=ag.scan_history_days)
    data_cfg = dataclasses.replace(cfg.data, start=start, cache_dir=cfg.data.cache_dir / "scan")
    prices = {t: df for t, df in universe_prices.items() if t in tickers}
    missing = [t for t in tickers if t not in prices]
    fetched, warnings = load_prices(missing, data_cfg, now=now)
    if warnings:
        log.info("Scan data: %d warning(s), e.g. %s", len(warnings), warnings[0])
    prices.update(fetched)
    return prices


def run_scan(cfg: Config, day: dt.date, universe_prices: dict[str, pd.DataFrame],
             now: dt.datetime | None = None) -> list[dict]:
    """Setups on `day`, best first. Stocks whose latest bar is not `day` are left out."""
    from papertrader.agent.scanner import scan_daily

    prices = {t: df.loc[:pd.Timestamp(day)] for t, df in scan_prices(cfg, universe_prices, now).items()}
    prices = {t: df for t, df in prices.items() if len(df) and df.index[-1].date() == day}
    return scan_daily(prices)
