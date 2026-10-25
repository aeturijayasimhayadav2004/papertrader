"""Rank strategies out-of-sample and keep only those that beat buy-and-hold.

Each strategy is backtested on the in-sample (train) and out-of-sample (test)
periods. A strategy qualifies for paper trading only if, on the test period and
net of costs, it beats equal-weight buy-and-hold of the same universe and made
enough trades for the result to mean something. Qualified strategies are ranked
by how much they beat buy-and-hold.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from papertrader.backtest import PortfolioResult, buy_and_hold, run_backtest
from papertrader.config import Config
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
                                 note=" ".join(ev.notes)))
        for period, res in bh.items():
            if res is not None:
                rows.append(_row(self.as_of, "buy_and_hold", period, res, res, qualified=None,
                                 overfit=None, rank=None, note="Equal-weight benchmark of the universe."))
        return rows


def _row(as_of: dt.date, name: str, period: str, res: PortfolioResult, bh: PortfolioResult | None,
         *, qualified: bool | None, overfit: bool | None, rank: int | None, note: str) -> dict[str, Any]:
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
        "win_rate": m["win_rate"],
        "n_trades": m["n_trades"],
        "exposure": m["exposure"],
        "bh_return": bh_ret,
        "excess_return": None if bh_ret is None else m["total_return"] - bh_ret,
        "qualified": qualified,
        "overfit_flag": overfit,
        "rank": rank,
        "note": note,
    }


def evaluate_strategies(
    strategies: list[Strategy], prices: dict[str, pd.DataFrame], cfg: Config, as_of: dt.date
) -> Selection:
    """Backtest every strategy in- and out-of-sample using data up to `as_of` only."""
    bt, sel = cfg.backtest, cfg.selection
    cost = cfg.costs.per_side
    stop = cfg.risk.stop_loss_pct if bt.apply_stop_loss else None
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
    for strat in strategies:
        res_is = run_backtest(strat, hist, *is_period, cfg.capital, cost, stop)
        res_oos = run_backtest(strat, hist, *oos_period, cfg.capital, cost, stop)
        m_is, m_oos = res_is.metrics, res_oos.metrics
        excess = m_oos["total_return"] - bh_oos.metrics["total_return"]
        notes = [
            f"Out-of-sample return {m_oos['total_return']:+.1%} vs buy-and-hold "
            f"{bh_oos.metrics['total_return']:+.1%} (net of {cfg.costs.per_side_pct:g}% costs per side)."
        ]
        beats = excess > 0
        enough = m_oos["n_trades"] >= sel.min_trades
        if not beats:
            notes.append("Did not beat buy-and-hold, so it will not trade.")
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
                                              excess_return=excess, notes=notes))

    for rank, ev in enumerate(sorted((e for e in evaluations if e.qualified),
                                     key=lambda e: e.excess_return, reverse=True), start=1):
        ev.rank = rank
    return Selection(as_of, periods, evaluations, bh_is, bh_oos)
