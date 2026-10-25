"""Rank strategies out-of-sample and keep only those that beat buy-and-hold.

Each strategy is backtested on the in-sample (train) and out-of-sample (test)
periods. A strategy qualifies for paper trading only if, on the test period and
net of costs, it beats equal-weight buy-and-hold of the same universe and made
enough trades for the result to mean something. Qualified strategies are ranked
by how much they beat buy-and-hold.

"Beats" uses `selection.criterion`: total return (default) or Sharpe ratio
(risk-adjusted). With `selection.method: walk_forward` the test period is also
cut into consecutive folds (e.g. 12 months each) and a strategy must beat
buy-and-hold in at least `wf_min_fold_win_rate` of them, so one lucky year
cannot carry it. Backtests use the same costs, exits and market-regime filter
as the paper broker.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from papertrader.backtest import PortfolioResult, buy_and_hold, market_regime, run_backtest
from papertrader.config import Config
from papertrader.exits import ExitRules
from papertrader.strategies.base import Strategy


@dataclass
class StrategyEvaluation:
    name: str
    label: str
    in_sample: PortfolioResult
    out_of_sample: PortfolioResult
    qualified: bool
    overfit_flag: bool
    excess_return: float
    rank: int | None = None
    notes: list[str] = field(default_factory=list)
    score: float = 0.0                   # criterion edge over buy-and-hold, used for ranking
    fold_win_rate: float | None = None   # walk-forward only


@dataclass
class Selection:
    as_of: dt.date
    periods: dict[str, tuple[dt.date, dt.date]]
    evaluations: list[StrategyEvaluation]
    bh_in_sample: PortfolioResult | None
    bh_out_of_sample: PortfolioResult | None
    problems: list[str] = field(default_factory=list)

    @property
    def selected(self) -> list[str]:
        ranked = sorted((e for e in self.evaluations if e.qualified), key=lambda e: e.rank or 0)
        return [e.name for e in ranked]

    def summary(self) -> str:
        if self.problems:
            return "No strategy selected: " + " ".join(self.problems) + " The bot holds cash."
        oos_start, oos_end = self.periods["out_of_sample"]
        if not self.selected:
            return (f"No strategy beat buy-and-hold out-of-sample ({oos_start} to {oos_end}) net of costs. "
                    "The bot holds cash.")
        labels = [e.label for e in sorted(self.evaluations, key=lambda e: e.rank or 99) if e.qualified]
        return f"Trading with (best first): {', '.join(labels)}."

    def to_state(self) -> dict[str, Any]:
        return {"as_of": self.as_of.isoformat(), "selected": self.selected, "summary": self.summary()}

    def to_rows(self) -> list[dict[str, Any]]:
        """One row per strategy (and buy-and-hold) per period, for the backtest_results table."""
        rows: list[dict[str, Any]] = []
        bh = {"in_sample": self.bh_in_sample, "out_of_sample": self.bh_out_of_sample}
        for ev in self.evaluations:
            for period, res in (("in_sample", ev.in_sample), ("out_of_sample", ev.out_of_sample)):
                rows.append(_row(self.as_of, ev.name, period, res, bh[period],
                                 qualified=ev.qualified, overfit=ev.overfit_flag, rank=ev.rank,
                                 note=" ".join(ev.notes),
                                 fold_win_rate=ev.fold_win_rate if period == "out_of_sample" else None))
        for period, res in bh.items():
            if res is not None:
                rows.append(_row(self.as_of, "buy_and_hold", period, res, res, qualified=None,
                                 overfit=None, rank=None, note="Equal-weight benchmark of the universe.",
                                 fold_win_rate=None))
        return rows


def _row(as_of: dt.date, name: str, period: str, res: PortfolioResult, bh: PortfolioResult | None,
         *, qualified: bool | None, overfit: bool | None, rank: int | None, note: str,
         fold_win_rate: float | None) -> dict[str, Any]:
    m = res.metrics
    bh_ret = bh.metrics["total_return"] if bh else None
    return {
        "id": f"{as_of.isoformat()}|{name}|{period}",
        "as_of_date": as_of,
        "strategy": name,
        "period": period,
        "start_date": res.start,
        "end_date": res.end,
        "total_return": m["total_return"],
        "cagr": m["cagr"],
        "max_drawdown": m["max_drawdown"],
        "sharpe": m["sharpe"],
        "sortino": m["sortino"],
        "calmar": m["calmar"],
        "profit_factor": m["profit_factor"],
        "win_rate": m["win_rate"],
        "n_trades": m["n_trades"],
        "exposure": m["exposure"],
        "bh_return": bh_ret,
        "excess_return": None if bh_ret is None else m["total_return"] - bh_ret,
        "qualified": qualified,
        "overfit_flag": overfit,
        "rank": rank,
        "note": note,
        "fold_win_rate": fold_win_rate,
    }


def _edge(m: dict[str, Any], bh: dict[str, Any], criterion: str) -> float:
    key = "sharpe" if criterion == "sharpe" else "total_return"
    return float(m[key] - bh[key])


def _folds(start: dt.date, end: dt.date, months: int) -> list[tuple[dt.date, dt.date]]:
    out, cur = [], pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    while cur <= end_ts:
        nxt = cur + pd.DateOffset(months=months)
        out.append((cur.date(), min(nxt - pd.Timedelta(days=1), end_ts).date()))
        cur = nxt
    # fold this short would be noise: merge it into the previous one
    if len(out) > 1 and (out[-1][1] - out[-1][0]).days < 90:
        out[-2:] = [(out[-2][0], out[-1][1])]
    return out


def evaluate_strategies(
    strategies: list[Strategy], prices: dict[str, pd.DataFrame], cfg: Config, as_of: dt.date,
    benchmark: pd.DataFrame | None = None,
) -> Selection:
    """Backtest every strategy in- and out-of-sample using data up to `as_of` only."""
    bt, sel = cfg.backtest, cfg.selection
    cost = cfg.costs.model()
    exits = cfg.risk.exit_rules() if bt.apply_stop_loss else ExitRules(stop_loss_pct=None)
    regime = None
    if cfg.regime.enabled and benchmark is not None:
        regime = market_regime(benchmark.loc[: pd.Timestamp(as_of)], cfg.regime.sma_window)
    is_period = (bt.train_start, min(bt.train_end, as_of))
    oos_period = (bt.test_start, min(bt.test_end or as_of, as_of))
    periods = {"in_sample": is_period, "out_of_sample": oos_period}

    hist = {t: df.loc[: pd.Timestamp(as_of)] for t, df in prices.items()}
    hist = {t: df for t, df in hist.items() if not df.empty}

    problems = []
    if not hist:
        problems.append("No price data.")
    elif oos_period[0] >= oos_period[1] or max(len(df.loc[pd.Timestamp(oos_period[0]):]) for df in hist.values()) < 20:
        problems.append(f"The out-of-sample period starting {oos_period[0]} has too little data.")
    if problems:
        return Selection(as_of, periods, [], None, None, problems)

    bh_is = buy_and_hold(hist, *is_period, cfg.capital, cost)
    bh_oos = buy_and_hold(hist, *oos_period, cfg.capital, cost)
    evaluations: list[StrategyEvaluation] = []
    folds = _folds(*oos_period, sel.wf_fold_months) if sel.method == "walk_forward" else []
    bh_folds = {f: buy_and_hold(hist, *f, cfg.capital, cost) for f in folds}
    for strat in strategies:
        res_is = run_backtest(strat, hist, *is_period, cfg.capital, cost, exits=exits, regime=regime)
        res_oos = run_backtest(strat, hist, *oos_period, cfg.capital, cost, exits=exits, regime=regime)
        m_is, m_oos = res_is.metrics, res_oos.metrics
        excess = m_oos["total_return"] - bh_oos.metrics["total_return"]
        score = _edge(m_oos, bh_oos.metrics, sel.criterion)
        notes = [
            f"Out-of-sample return {m_oos['total_return']:+.1%} vs buy-and-hold "
            f"{bh_oos.metrics['total_return']:+.1%}; Sharpe {m_oos['sharpe']:.2f} vs "
            f"{bh_oos.metrics['sharpe']:.2f} (net of costs: {cfg.costs.describe()})."
        ]
        beats = score > 0
        enough = m_oos["n_trades"] >= sel.min_trades
        if not beats:
            what = "risk-adjusted (Sharpe)" if sel.criterion == "sharpe" else "on total return"
            notes.append(f"Did not beat buy-and-hold {what}, so it will not trade.")
        fold_rate = None
        if folds:
            wins, parts = 0, []
            for f in folds:
                r = run_backtest(strat, hist, *f, cfg.capital, cost, exits=exits, regime=regime)
                b = bh_folds[f]
                won = _edge(r.metrics, b.metrics, sel.criterion) > 0
                wins += won
                parts.append(f"{f[0]:%b %Y}-{f[1]:%b %Y} {r.metrics['total_return']:+.1%} vs "
                             f"{b.metrics['total_return']:+.1%}{' (won)' if won else ''}")
            fold_rate = wins / len(folds)
            notes.append(f"Walk-forward: beat buy-and-hold in {wins}/{len(folds)} folds ({'; '.join(parts)}).")
            if fold_rate < sel.wf_min_fold_win_rate:
                beats = False
                notes.append(f"Fold win rate {fold_rate:.0%} is below the required "
                             f"{sel.wf_min_fold_win_rate:.0%}: not consistent enough to trade.")
        if not enough:
            notes.append(f"Only {m_oos['n_trades']} out-of-sample trades (minimum {sel.min_trades}): too few to trust.")

        overfit = False
        if m_is["sharpe"] > 0 and m_oos["sharpe"] < sel.overfit_sharpe_ratio_min * m_is["sharpe"]:
            overfit = True
            notes.append(f"Sharpe fell from {m_is['sharpe']:.2f} in-sample to {m_oos['sharpe']:.2f} "
                         "out-of-sample: possible overfitting.")
        gap = (m_is["cagr"] - m_oos["cagr"]) * 100
        if gap > sel.overfit_cagr_gap_pct:
            overfit = True
            notes.append(f"Annual return fell by {gap:.1f} points out-of-sample: possible overfitting.")

        evaluations.append(StrategyEvaluation(strat.name, strat.label, res_is, res_oos,
                                              qualified=beats and enough, overfit_flag=overfit,
                                              excess_return=excess, notes=notes, score=score,
                                              fold_win_rate=fold_rate))

    for rank, ev in enumerate(sorted((e for e in evaluations if e.qualified),
                                     key=lambda e: e.score, reverse=True), start=1):
        ev.rank = rank
    return Selection(as_of, periods, evaluations, bh_is, bh_oos)
