from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from papertrader.config import parse_config


def make_ohlc(closes, start="2020-01-01", opens=None, lows=None, highs=None) -> pd.DataFrame:
    """Build a daily OHLCV frame on business days. Opens default to the previous close."""
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range(start, periods=len(closes), name="Date")
    if opens is None:
        opens = np.concatenate([[closes[0]], closes[:-1]])
    opens = np.asarray(opens, dtype=float)
    hi = np.maximum(opens, closes) * 1.005 if highs is None else np.asarray(highs, float)
    lo = np.minimum(opens, closes) * 0.995 if lows is None else np.asarray(lows, float)
    return pd.DataFrame({"Open": opens, "High": hi, "Low": lo, "Close": closes, "Volume": 1e6}, index=idx)


def random_walk(n=600, seed=0, start="2018-01-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, n)))
    opens = closes * np.exp(rng.normal(0, 0.004, n))
    hi = np.maximum(opens, closes) * (1 + rng.uniform(0, 0.01, n))
    lo = np.minimum(opens, closes) * (1 - rng.uniform(0, 0.01, n))
    return make_ohlc(closes, start=start, opens=opens, lows=lo, highs=hi)


RAW_CONFIG = {
    "capital": 1_000_000,
    "universe": ["AAA.NS", "BBB.NS"],
    "benchmark": "^NSEI",
    "data": {"start": "2018-01-01", "cache_dir": "data/test_cache", "cache_ttl_hours": 6,
             "market_tz": "Asia/Kolkata", "market_close_cutoff": "15:45"},
    "costs": {"per_side_pct": 0.2},
    "backtest": {"train_start": "2018-01-01", "train_end": "2019-06-30", "test_start": "2019-07-01",
                 "test_end": None, "apply_stop_loss": True},
    "selection": {"rerank_every_days": 30, "min_trades": 1, "overfit_sharpe_ratio_min": 0.5,
                  "overfit_cagr_gap_pct": 15},
    "strategies": {"ma_crossover": {"fast": 5, "slow": 20}},
    "risk": {"max_position_pct": 10, "stop_loss_pct": 5, "max_open_positions": 5, "max_daily_loss_pct": 2},
    "storage": {"sqlite_path": "data/test.db", "use_supabase": False},
    "logging": {"dir": "logs"},
}


@pytest.fixture
def cfg(tmp_path):
    raw = {**RAW_CONFIG, "storage": {"sqlite_path": str(tmp_path / "pt.db"), "use_supabase": False},
           "data": {**RAW_CONFIG["data"], "cache_dir": str(tmp_path / "cache")},
           "logging": {"dir": str(tmp_path / "logs")}}
    return parse_config(raw)
