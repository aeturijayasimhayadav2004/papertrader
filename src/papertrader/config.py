"""Load config.yaml into typed, validated settings."""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class ConfigError(ValueError):
    pass


def _date(value: Any, name: str) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"{name}: expected YYYY-MM-DD, got {value!r}") from exc


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _positive(value: Any, name: str) -> float:
    v = float(value)
    if v <= 0:
        raise ConfigError(f"{name} must be > 0, got {value!r}")
    return v


@dataclass(frozen=True)
class DataConfig:
    start: dt.date
    cache_dir: Path
    cache_ttl_hours: float
    market_tz: str
    market_close_cutoff: dt.time


@dataclass(frozen=True)
class CostConfig:
    per_side_pct: float

    @property
    def per_side(self) -> float:
        return self.per_side_pct / 100.0


@dataclass(frozen=True)
class BacktestConfig:
    train_start: dt.date
    train_end: dt.date
    test_start: dt.date
    test_end: dt.date | None
    apply_stop_loss: bool


@dataclass(frozen=True)
class SelectionConfig:
    rerank_every_days: int
    min_trades: int
    overfit_sharpe_ratio_min: float
    overfit_cagr_gap_pct: float


@dataclass(frozen=True)
class RiskConfig:
    max_position_pct: float
    stop_loss_pct: float
    max_open_positions: int
    max_daily_loss_pct: float


@dataclass(frozen=True)
class StorageConfig:
    sqlite_path: Path
    use_supabase: bool
    require_supabase: bool


@dataclass(frozen=True)
class Config:
    capital: float
    universe: list[str]
    benchmark: str
    data: DataConfig
    costs: CostConfig
    backtest: BacktestConfig
    selection: SelectionConfig
    risk: RiskConfig
    storage: StorageConfig
    log_dir: Path
    strategies: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("PAPERTRADER_CONFIG") or DEFAULT_CONFIG_PATH)
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> Config:
    try:
        d, c, b, s, r, st = (raw[k] for k in ("data", "costs", "backtest", "selection", "risk", "storage"))
    except KeyError as exc:
        raise ConfigError(f"missing config section: {exc.args[0]}") from exc

    universe = [str(t).strip() for t in raw.get("universe") or []]
    if not universe:
        raise ConfigError("universe must list at least one ticker")
    if len(set(universe)) != len(universe):
        raise ConfigError("universe contains duplicate tickers")

    hh, mm = str(d.get("market_close_cutoff", "15:45")).split(":")
    data = DataConfig(
        start=_date(d["start"], "data.start"),
        cache_dir=_path(d.get("cache_dir", "data/cache")),
        cache_ttl_hours=float(d.get("cache_ttl_hours", 6)),
        market_tz=d.get("market_tz", "Asia/Kolkata"),
        market_close_cutoff=dt.time(int(hh), int(mm)),
    )
    costs = CostConfig(per_side_pct=float(c["per_side_pct"]))
    if not 0 <= costs.per_side_pct < 5:
        raise ConfigError("costs.per_side_pct must be between 0 and 5")

    backtest = BacktestConfig(
        train_start=_date(b["train_start"], "backtest.train_start"),
        train_end=_date(b["train_end"], "backtest.train_end"),
        test_start=_date(b["test_start"], "backtest.test_start"),
        test_end=_date(b.get("test_end"), "backtest.test_end"),
        apply_stop_loss=bool(b.get("apply_stop_loss", True)),
    )
    if not backtest.train_start < backtest.train_end < backtest.test_start:
        raise ConfigError("need train_start < train_end < test_start (no overlap between train and test)")
    if backtest.test_end and backtest.test_end <= backtest.test_start:
        raise ConfigError("backtest.test_end must be after test_start")

    selection = SelectionConfig(
        rerank_every_days=int(s.get("rerank_every_days", 30)),
        min_trades=int(s.get("min_trades", 5)),
        overfit_sharpe_ratio_min=float(s.get("overfit_sharpe_ratio_min", 0.5)),
        overfit_cagr_gap_pct=float(s.get("overfit_cagr_gap_pct", 15)),
    )
    risk = RiskConfig(
        max_position_pct=_positive(r["max_position_pct"], "risk.max_position_pct"),
        stop_loss_pct=_positive(r["stop_loss_pct"], "risk.stop_loss_pct"),
        max_open_positions=int(_positive(r["max_open_positions"], "risk.max_open_positions")),
        max_daily_loss_pct=_positive(r["max_daily_loss_pct"], "risk.max_daily_loss_pct"),
    )
    if risk.max_position_pct > 100 or risk.stop_loss_pct >= 100:
        raise ConfigError("risk percentages must be below 100")

    storage = StorageConfig(
        sqlite_path=_path(st.get("sqlite_path", "data/papertrader.db")),
        use_supabase=bool(st.get("use_supabase", True)),
        require_supabase=bool(st.get("require_supabase", False))
        or os.environ.get("PAPERTRADER_REQUIRE_SUPABASE") == "1",
    )

    return Config(
        capital=_positive(raw["capital"], "capital"),
        universe=universe,
        benchmark=str(raw.get("benchmark", "^NSEI")),
        data=data,
        costs=costs,
        backtest=backtest,
        selection=selection,
        risk=risk,
        storage=storage,
        log_dir=_path((raw.get("logging") or {}).get("dir", "logs")),
        strategies=dict(raw.get("strategies") or {}),
    )
