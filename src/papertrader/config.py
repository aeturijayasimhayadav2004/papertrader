"""Load config.yaml into typed, validated settings."""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from papertrader.costs import CostModel, flat, india_delivery
from papertrader.exits import ExitRules

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


def _opt_positive(value, name: str) -> float | None:
    return None if value in (None, "", 0) else _positive(value, name)


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
    model_name: str = "flat"                  # flat | india_delivery
    india: dict[str, Any] = field(default_factory=dict)

    @property
    def per_side(self) -> float:
        return self.per_side_pct / 100.0

    def model(self) -> CostModel:
        if self.model_name == "india_delivery":
            return india_delivery(**self.india)
        return flat(self.per_side)

    def describe(self) -> str:
        m = self.model()
        return f"{m.label}, about {m.round_trip_pct():.2%} per round trip on Rs 1 lakh"


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
    method: str = "holdout"          # holdout | walk_forward
    criterion: str = "return"        # return | sharpe  (what "beats buy-and-hold" means)
    wf_fold_months: int = 12
    wf_min_fold_win_rate: float = 0.5


@dataclass(frozen=True)
class RiskConfig:
    max_position_pct: float
    stop_loss_pct: float
    max_open_positions: int
    max_daily_loss_pct: float
    stop_mode: str = "fixed"                 # fixed | atr
    atr_period: int = 14
    atr_multiple: float = 2.5
    trailing_stop_pct: float | None = None
    take_profit_pct: float | None = None
    take_profit_r: float | None = None       # target = entry + R x (entry - initial stop)
    sizing: str = "fixed_pct"                # fixed_pct | risk_based
    risk_per_trade_pct: float = 1.0
    max_new_orders_per_day: int = 5
    max_price_jump_pct: float = 20.0         # bar-to-bar move treated as a data error

    def exit_rules(self) -> ExitRules:
        return ExitRules(self.stop_loss_pct, self.stop_mode, self.atr_multiple, self.trailing_stop_pct,
                         self.take_profit_pct, self.atr_period, take_profit_r=self.take_profit_r)


@dataclass(frozen=True)
class RegimeConfig:
    enabled: bool = False
    sma_window: int = 200


@dataclass(frozen=True)
class ProtectionConfig:
    """Freqtrade-style circuit breakers. 0 / None disables a rule."""
    cooldown_days: int = 0                   # no re-entry into a stock for N days after an exit
    stoploss_guard_lookback_days: int = 0    # N stop-losses within the lookback ...
    stoploss_guard_max_stops: int = 3
    stoploss_guard_pause_days: int = 5       # ... pause all new buys this long
    max_drawdown_pct: float | None = None    # drawdown within the lookback window ...
    max_drawdown_lookback_days: int = 60
    max_drawdown_pause_days: int = 10        # ... pause all new buys this long
    low_profit_lookback_trades: int = 0      # last N trades of a stock net negative ...
    low_profit_lock_days: int = 20           # ... lock that stock this long


@dataclass(frozen=True)
class TradingConfig:
    mode: str = "paper"                      # only "paper" is implemented; see docs/ARCHITECTURE.md
    kill_switch_file: Path = PROJECT_ROOT / "KILL_SWITCH"


@dataclass(frozen=True)
class NotifyConfig:
    telegram: bool = True                    # sends only when TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set


AGENT_DESKS = ("events", "news", "insider", "analyst", "odds")


@dataclass(frozen=True)
class AgentConfig:
    """The trading agent's desks. Each listed desk is asked about every buy candidate;
    a BLOCK finding stops that buy. A desk whose data is unreachable reports
    "unknown", which blocks only when block_on_unknown is true."""
    enabled: bool = False
    desks: tuple[str, ...] = ("events", "news", "insider", "odds")
    block_on_unknown: bool = False
    results_days_before: int = 2             # no new buy this many trading days before a results meeting ...
    results_days_after: int = 1              # ... or this many after it
    news_hours: int = 48
    insider_days: int = 30
    odds_min_signals: int = 8                # past signals needed before the odds desk can block
    odds_min_win_rate: float = 0.40
    odds_lookback_days: int = 1500
    scan_universe: str = "nifty50"           # none | config | nifty50 | nifty100 | nifty500
    scan_history_days: int = 400
    scan_top_n: int = 25


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
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    protections: ProtectionConfig = field(default_factory=ProtectionConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    # Raw sections parsed by their own modules (each has a from_dict with defaults):
    # brain -> agent.brain.BrainConfig, intraday -> agent.intraday_runner.IntradayConfig,
    # portfolio -> rotation strategy settings, learning -> agent.learning options.
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)


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
    costs = CostConfig(per_side_pct=float(c.get("per_side_pct", 0.2)), model_name=str(c.get("model", "flat")),
                       india=dict(c.get("india") or {}))
    if not 0 <= costs.per_side_pct < 5:
        raise ConfigError("costs.per_side_pct must be between 0 and 5")
    if costs.model_name not in ("flat", "india_delivery"):
        raise ConfigError("costs.model must be 'flat' or 'india_delivery'")
    try:
        costs.model()
    except TypeError as exc:
        raise ConfigError(f"costs.india: {exc}") from exc

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
        method=str(s.get("method", "holdout")),
        criterion=str(s.get("criterion", "return")),
        wf_fold_months=int(s.get("wf_fold_months", 12)),
        wf_min_fold_win_rate=float(s.get("wf_min_fold_win_rate", 0.5)),
    )
    if selection.method not in ("holdout", "walk_forward"):
        raise ConfigError("selection.method must be 'holdout' or 'walk_forward'")
    if selection.criterion not in ("return", "sharpe"):
        raise ConfigError("selection.criterion must be 'return' or 'sharpe'")
    if selection.wf_fold_months < 1 or not 0 <= selection.wf_min_fold_win_rate <= 1:
        raise ConfigError("selection.wf_fold_months must be >= 1 and wf_min_fold_win_rate in [0, 1]")
    risk = RiskConfig(
        max_position_pct=_positive(r["max_position_pct"], "risk.max_position_pct"),
        stop_loss_pct=_positive(r["stop_loss_pct"], "risk.stop_loss_pct"),
        max_open_positions=int(_positive(r["max_open_positions"], "risk.max_open_positions")),
        max_daily_loss_pct=_positive(r["max_daily_loss_pct"], "risk.max_daily_loss_pct"),
        stop_mode=str(r.get("stop_mode", "fixed")),
        atr_period=int(r.get("atr_period", 14)),
        atr_multiple=float(r.get("atr_multiple", 2.5)),
        trailing_stop_pct=_opt_positive(r.get("trailing_stop_pct"), "risk.trailing_stop_pct"),
        take_profit_pct=_opt_positive(r.get("take_profit_pct"), "risk.take_profit_pct"),
        take_profit_r=_opt_positive(r.get("take_profit_r"), "risk.take_profit_r"),
        sizing=str(r.get("sizing", "fixed_pct")),
        risk_per_trade_pct=_positive(r.get("risk_per_trade_pct", 1.0), "risk.risk_per_trade_pct"),
        max_new_orders_per_day=int(_positive(r.get("max_new_orders_per_day", 5), "risk.max_new_orders_per_day")),
        max_price_jump_pct=_positive(r.get("max_price_jump_pct", 20.0), "risk.max_price_jump_pct"),
    )
    if risk.max_position_pct > 100 or risk.stop_loss_pct >= 100:
        raise ConfigError("risk percentages must be below 100")
    if risk.stop_mode not in ("fixed", "atr"):
        raise ConfigError("risk.stop_mode must be 'fixed' or 'atr'")
    if risk.sizing not in ("fixed_pct", "risk_based"):
        raise ConfigError("risk.sizing must be 'fixed_pct' or 'risk_based'")
    if (risk.trailing_stop_pct or 0) >= 100:
        raise ConfigError("risk.trailing_stop_pct must be below 100")

    rg = raw.get("regime") or {}
    regime = RegimeConfig(enabled=bool(rg.get("enabled", False)), sma_window=int(rg.get("sma_window", 200)))

    pr = raw.get("protections") or {}
    sg, md, lp = pr.get("stoploss_guard") or {}, pr.get("max_drawdown") or {}, pr.get("low_profit") or {}
    protections = ProtectionConfig(
        cooldown_days=int(pr.get("cooldown_days", 0)),
        stoploss_guard_lookback_days=int(sg.get("lookback_days", 0)),
        stoploss_guard_max_stops=int(sg.get("max_stops", 3)),
        stoploss_guard_pause_days=int(sg.get("pause_days", 5)),
        max_drawdown_pct=_opt_positive(md.get("max_drawdown_pct"), "protections.max_drawdown.max_drawdown_pct"),
        max_drawdown_lookback_days=int(md.get("lookback_days", 60)),
        max_drawdown_pause_days=int(md.get("pause_days", 10)),
        low_profit_lookback_trades=int(lp.get("lookback_trades", 0)),
        low_profit_lock_days=int(lp.get("lock_days", 20)),
    )

    tr = raw.get("trading") or {}
    trading = TradingConfig(mode=str(tr.get("mode", "paper")),
                            kill_switch_file=_path(tr.get("kill_switch_file", "KILL_SWITCH")))
    if trading.mode != "paper":
        raise ConfigError("trading.mode: only 'paper' is implemented. Live trading needs the broker adapter, "
                          "reconciliation and shadow-mode steps in docs/ARCHITECTURE.md first.")
    notify = NotifyConfig(telegram=bool((raw.get("notify") or {}).get("telegram", True)))

    ag = raw.get("agent") or {}
    agent = AgentConfig(
        enabled=bool(ag.get("enabled", False)),
        desks=tuple(str(x) for x in ag.get("desks", AgentConfig.desks)),
        block_on_unknown=bool(ag.get("block_on_unknown", False)),
        results_days_before=int(ag.get("results_days_before", 2)),
        results_days_after=int(ag.get("results_days_after", 1)),
        news_hours=int(ag.get("news_hours", 48)),
        insider_days=int(ag.get("insider_days", 30)),
        odds_min_signals=int(ag.get("odds_min_signals", 8)),
        odds_min_win_rate=float(ag.get("odds_min_win_rate", 0.40)),
        odds_lookback_days=int(ag.get("odds_lookback_days", 1500)),
        scan_universe=str(ag.get("scan_universe", "nifty50")),
        scan_history_days=int(ag.get("scan_history_days", 400)),
        scan_top_n=int(ag.get("scan_top_n", 25)),
    )
    bad = set(agent.desks) - set(AGENT_DESKS)
    if bad:
        raise ConfigError(f"agent.desks: unknown desk(s) {sorted(bad)}; choose from {list(AGENT_DESKS)}")
    if agent.scan_universe not in ("none", "config", "nifty50", "nifty100", "nifty500"):
        raise ConfigError("agent.scan_universe must be none, config, nifty50, nifty100 or nifty500")
    if not 0 <= agent.odds_min_win_rate <= 1:
        raise ConfigError("agent.odds_min_win_rate must be between 0 and 1")

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
        regime=regime,
        protections=protections,
        trading=trading,
        notify=notify,
        agent=agent,
        sections={k: dict(raw.get(k) or {}) for k in ("brain", "intraday", "portfolio", "learning")},
    )
