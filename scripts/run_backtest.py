"""Backtest every strategy in- and out-of-sample and show which ones would trade.

Usage:
    python scripts/run_backtest.py            # uses cached data if fresh
    python scripts/run_backtest.py --refresh  # force re-download
Outputs a table to the console and CSV + charts in reports/.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from papertrader.config import PROJECT_ROOT, load_config  # noqa: E402
from papertrader.data import load_prices  # noqa: E402
from papertrader.logs import setup_logging  # noqa: E402
from papertrader.metrics import monte_carlo  # noqa: E402
from papertrader.selection import evaluate_strategies  # noqa: E402
from papertrader.strategies import build_strategies  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and re-download")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.log_dir)
    loaded, warnings = load_prices(list(dict.fromkeys(cfg.universe + [cfg.benchmark])), cfg.data,
                                   refresh=args.refresh)
    prices = {t: df for t, df in loaded.items() if t in cfg.universe}
    if not prices:
        print("No price data available.", *warnings, sep="\n")
        return 1
    as_of = max(df.index[-1] for df in prices.values()).date()
    selection = evaluate_strategies(build_strategies(cfg.strategies), prices, cfg, as_of,
                                    benchmark=loaded.get(cfg.benchmark))

    print("\nPAPER TRADING BACKTEST - NO REAL MONEY")
    print(f"Data up to {as_of}. Costs: {cfg.costs.describe()}.")
    print(f"Exits in backtest: {'on' if cfg.backtest.apply_stop_loss else 'off'} ({cfg.risk.exit_rules()}).")
    print(f"Regime filter: {'on' if cfg.regime.enabled else 'off'}. Selection: {cfg.selection.method}, "
          f"criterion {cfg.selection.criterion}.")
    for period, (s, e) in selection.periods.items():
        print(f"  {period:14s} {s} -> {e}")

    rows = pd.DataFrame(selection.to_rows())
    if rows.empty:
        print("\n" + selection.summary())
        return 0
    show = rows[["strategy", "period", "total_return", "cagr", "max_drawdown", "sharpe", "sortino", "calmar",
                 "profit_factor", "win_rate", "n_trades", "exposure", "bh_return", "fold_win_rate",
                 "qualified", "overfit_flag"]].copy()
    for col in ["total_return", "cagr", "max_drawdown", "win_rate", "exposure", "bh_return", "fold_win_rate"]:
        show[col] = show[col].map(lambda v: "" if pd.isna(v) else f"{v:+.1%}")
    for col in ["sharpe", "sortino", "calmar", "profit_factor"]:
        show[col] = show[col].map(lambda v: "" if pd.isna(v) else f"{v:.2f}")
    print("\n" + show.to_string(index=False))

    print("\nNotes:")
    for ev in selection.evaluations:
        flag = "QUALIFIED" if ev.qualified else "not selected"
        print(f"  - {ev.label} [{flag}{', OVERFIT WARNING' if ev.overfit_flag else ''}]: {' '.join(ev.notes)}")
    print("\nMonte Carlo (2,000 resamples of out-of-sample trades, each on a 1/N sleeve):")
    for ev in selection.evaluations:
        mc = monte_carlo([t.return_pct for t in ev.out_of_sample.trades], fraction=1 / len(prices))
        if mc:
            print(f"  - {ev.label}: P(loss) {mc['prob_loss']:.0%}, 5th pct return {mc['return_p5']:+.1%}, "
                  f"median {mc['return_p50']:+.1%}, bad-case drawdown {mc['max_drawdown_p95']:.1%}")
    print("\n" + selection.summary())

    out = PROJECT_ROOT / "reports"
    out.mkdir(exist_ok=True)
    rows.to_csv(out / "backtest_summary.csv", index=False)
    for period in ("in_sample", "out_of_sample"):
        fig, ax = plt.subplots(figsize=(11, 5))
        for ev in selection.evaluations:
            res = ev.in_sample if period == "in_sample" else ev.out_of_sample
            ax.plot(res.equity.index, res.equity / cfg.capital, label=ev.label)
        bh = selection.bh_in_sample if period == "in_sample" else selection.bh_out_of_sample
        ax.plot(bh.equity.index, bh.equity / cfg.capital, label="Buy & hold (equal weight)", color="black",
                linestyle="--")
        ax.set_title(f"{period.replace('_', ' ').title()} growth of 1 (net of costs) - PAPER BACKTEST")
        ax.grid(alpha=0.3)
        ax.legend()
        fig.tight_layout()
        fig.savefig(out / f"equity_{period}.png", dpi=110)
        plt.close(fig)
    print(f"\nSaved {out / 'backtest_summary.csv'} and equity charts at {dt.datetime.now():%H:%M}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
