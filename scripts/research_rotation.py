"""Research: do portfolio-level rotation strategies beat buy-and-hold on NSE stocks?

Every candidate in papertrader.portfolio.rotation.REGISTRY is backtested on the
in-sample period (backtest.train_start..train_end, 2015-2021) and on the
out-of-sample period (backtest.test_start..latest bar), plus 12-month
walk-forward folds, against equal-weight buy-and-hold of the same universe and
against the Nifty 50 index (^NSEI, price only). Costs are the project's cost
model (NSE delivery, itemised). Parameters are fixed a priori; nothing here is
tuned.

A candidate PASSES if, out-of-sample and net of costs, it beats equal-weight
buy-and-hold on total return AND wins at least selection.wf_min_fold_win_rate
of the folds (the same bar the per-stock strategies face).

Usage:
    python scripts/research_rotation.py                         # nifty100
    python scripts/research_rotation.py --universe nifty100 nifty50
    python scripts/research_rotation.py --refresh               # force re-download
Writes reports/rotation_research.md, reports/rotation_research.csv and
reports/rotation_equity_<universe>.png.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from papertrader.agent.universe import load_universe  # noqa: E402
from papertrader.config import PROJECT_ROOT, load_config  # noqa: E402
from papertrader.data import load_prices  # noqa: E402
from papertrader.logs import setup_logging  # noqa: E402
from papertrader.portfolio import (  # noqa: E402
    REGISTRY,
    backtest_rotation,
    build_rotation,
    buy_and_hold_universe,
    index_buy_and_hold,
    walk_forward,
)

BAND_PCT = 1.0  # skip top-ups / trims under 1% of equity (fixed a priori)


def pct(v: float | None, sign: bool = True) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{v:+.1%}" if sign else f"{v:.1%}"


def run_universe(name: str, cfg, refresh: bool) -> tuple[list[dict], dict, dict]:
    """Backtest every candidate on one universe. Returns (rows, fold details, equity curves)."""
    tickers = load_universe(name)
    t0 = time.time()
    loaded, warnings = load_prices(list(dict.fromkeys(tickers + [cfg.benchmark])), cfg.data, refresh=refresh)
    bench = loaded.pop(cfg.benchmark, None)
    prices = {t: df for t, df in loaded.items() if t in tickers}
    print(f"[{name}] {len(prices)}/{len(tickers)} stocks loaded in {time.time() - t0:.0f}s"
          + (f"; {len(warnings)} warnings" if warnings else ""))
    if bench is None:
        raise SystemExit(f"No data for the benchmark {cfg.benchmark}")
    latest = min(bench.index[-1], max(df.index[-1] for df in prices.values()))
    bt, sel = cfg.backtest, cfg.selection
    periods = {"in_sample": (pd.Timestamp(bt.train_start), pd.Timestamp(bt.train_end)),
               "out_of_sample": (pd.Timestamp(bt.test_start), pd.Timestamp(bt.test_end or latest.date()))}
    periods["out_of_sample"] = (periods["out_of_sample"][0], min(periods["out_of_sample"][1], latest))
    cost = cfg.costs.model()
    capital = cfg.capital

    rows: list[dict] = []
    folds: dict[str, list[dict]] = {}
    curves: dict[str, pd.Series] = {}
    bh, idx = {}, {}
    for p, (s, e) in periods.items():
        bh[p] = buy_and_hold_universe(prices, s, e, cost, capital)
        idx[p] = index_buy_and_hold(bench, s, e, capital)
    bh_wf = {p: walk_forward_bh(prices, bench, s, e, cost, capital, sel.wf_fold_months) for p, (s, e) in periods.items()}
    curves["Buy & hold (equal weight)"] = bh["out_of_sample"].equity
    curves["Nifty 50 index (price)"] = idx["out_of_sample"].equity

    for key in REGISTRY:
        strat = build_rotation(key)
        cache: dict = {}
        res = {p: backtest_rotation(strat, prices, s, e, cost, capital, bench, BAND_PCT, cache)
               for p, (s, e) in periods.items()}
        wf = {p: walk_forward(strat, prices, s, e, cost, capital, bench, sel.wf_fold_months, BAND_PCT, cache)
              for p, (s, e) in periods.items()}
        folds[key] = wf["out_of_sample"].folds
        curves[strat.label] = res["out_of_sample"].equity
        m_is, m_oos = res["in_sample"].metrics, res["out_of_sample"].metrics
        excess = m_oos["total_return"] - bh["out_of_sample"].metrics["total_return"]
        fold_rate = wf["out_of_sample"].win_rate
        enough = m_oos["n_trades"] >= sel.min_trades
        passed = bool(excess > 0 and fold_rate >= sel.wf_min_fold_win_rate and enough)
        overfit_sharpe = m_is["sharpe"] > 0 and m_oos["sharpe"] < sel.overfit_sharpe_ratio_min * m_is["sharpe"]
        overfit_cagr = (m_is["cagr"] - m_oos["cagr"]) * 100 > sel.overfit_cagr_gap_pct
        row = {
            "universe": name, "strategy": key, "label": strat.label,
            "n_stocks": len(prices),
            "is_start": periods["in_sample"][0].date(), "is_end": periods["in_sample"][1].date(),
            "oos_start": periods["out_of_sample"][0].date(), "oos_end": periods["out_of_sample"][1].date(),
        }
        for p, tag in (("in_sample", "is"), ("out_of_sample", "oos")):
            m, b, ix = res[p].metrics, bh[p].metrics, idx[p].metrics
            row.update({
                f"{tag}_total_return": m["total_return"], f"{tag}_cagr": m["cagr"],
                f"{tag}_sharpe": m["sharpe"], f"{tag}_sortino": m["sortino"], f"{tag}_calmar": m["calmar"],
                f"{tag}_max_drawdown": m["max_drawdown"],
                f"{tag}_bh_total_return": b["total_return"], f"{tag}_bh_cagr": b["cagr"],
                f"{tag}_bh_sharpe": b["sharpe"], f"{tag}_bh_max_drawdown": b["max_drawdown"],
                f"{tag}_excess_vs_bh": m["total_return"] - b["total_return"],
                f"{tag}_cagr_excess_vs_bh": m["cagr"] - b["cagr"],
                f"{tag}_index_total_return": ix["total_return"], f"{tag}_index_cagr": ix["cagr"],
                f"{tag}_excess_vs_index": m["total_return"] - ix["total_return"],
                f"{tag}_fold_win_rate": wf[p].win_rate, f"{tag}_folds": len(wf[p].folds),
                f"{tag}_turnover": res[p].turnover, f"{tag}_fees_pct_capital": res[p].fees / capital,
                f"{tag}_exposure": res[p].exposure, f"{tag}_n_fills": m["n_trades"],
            })
        row.update({"overfit_flag": bool(overfit_sharpe or overfit_cagr),
                    "overfit_sharpe": bool(overfit_sharpe), "overfit_cagr": bool(overfit_cagr),
                    "passed": passed, "bh_fold_returns": "; ".join(f"{pct(r)}" for r in bh_wf["out_of_sample"])})
        rows.append(row)
        print(f"  {key:22s} IS {pct(m_is['cagr'])}/yr Sharpe {m_is['sharpe']:.2f} | OOS {pct(m_oos['cagr'])}/yr "
              f"Sharpe {m_oos['sharpe']:.2f} vs B&H {pct(bh['out_of_sample'].metrics['cagr'])}/yr | "
              f"excess {pct(excess)} | folds {wf['out_of_sample'].wins}/{len(wf['out_of_sample'].folds)} | "
              f"{'PASS' if passed else 'fail'}{' OVERFIT?' if row['overfit_flag'] else ''}")

    bench_rows = {"bh": bh, "index": idx}
    return rows, folds, {"curves": curves, "bench": bench_rows, "periods": periods, "n_stocks": len(prices)}


def walk_forward_bh(prices, bench, s, e, cost, capital, months) -> list[float]:
    from papertrader.selection import _folds
    return [buy_and_hold_universe(prices, fs, fe, cost, capital).metrics["total_return"]
            for fs, fe in _folds(s.date(), e.date(), months)]


def plot(name: str, curves: dict[str, pd.Series], capital: float, out: Path) -> Path:
    fig, ax = plt.subplots(figsize=(11, 5.5))
    for label, eq in curves.items():
        style = {"color": "black", "linestyle": "--"} if label.startswith("Buy") else (
            {"color": "grey", "linestyle": ":"} if label.startswith("Nifty") else {})
        ax.plot(eq.index, eq / capital, label=label, linewidth=1.3, **style)
    ax.set_title(f"{name}: out-of-sample growth of 1, net of costs (PAPER BACKTEST, survivorship-biased)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    path = out / f"rotation_equity_{name}.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def verdict(r: dict, min_fold: float) -> str:
    bits = []
    if r["passed"]:
        bits.append(f"PASSES the bar: beat buy-and-hold by {pct(r['oos_excess_vs_bh'])} total return out-of-sample "
                    f"({pct(r['oos_cagr'])}/yr vs {pct(r['oos_bh_cagr'])}/yr) and won "
                    f"{r['oos_fold_win_rate']:.0%} of {r['oos_folds']} yearly folds.")
    else:
        why = []
        if r["oos_excess_vs_bh"] <= 0:
            why.append(f"trailed buy-and-hold by {pct(-r['oos_excess_vs_bh'], False)} out-of-sample")
        if r["oos_fold_win_rate"] < min_fold:
            why.append(f"won only {r['oos_fold_win_rate']:.0%} of {r['oos_folds']} folds")
        bits.append("Fails: " + " and ".join(why) + ".")
    if r["is_excess_vs_bh"] > 0:
        bits.append(f"In-sample it also beat buy-and-hold ({pct(r['is_cagr'])}/yr vs {pct(r['is_bh_cagr'])}/yr, "
                    f"{r['is_fold_win_rate']:.0%} of in-sample folds).")
    else:
        bits.append(f"In-sample it trailed buy-and-hold ({pct(r['is_cagr'])}/yr vs {pct(r['is_bh_cagr'])}/yr), "
                    "so an out-of-sample win is less convincing.")
    if r["oos_sharpe"] > r["oos_bh_sharpe"]:
        bits.append(f"Risk-adjusted it was better too (Sharpe {r['oos_sharpe']:.2f} vs {r['oos_bh_sharpe']:.2f}).")
    else:
        bits.append(f"Risk-adjusted it was not better (Sharpe {r['oos_sharpe']:.2f} vs {r['oos_bh_sharpe']:.2f}).")
    if r["overfit_flag"]:
        bits.append("OVERFIT WARNING: " + ("OOS Sharpe < 0.5 x IS Sharpe. " if r["overfit_sharpe"] else "")
                    + ("CAGR fell > 15 points OOS." if r["overfit_cagr"] else ""))
    bits.append(f"Turnover {r['oos_turnover']:.1f}x a year; charges cost {pct(r['oos_fees_pct_capital'], False)} "
                "of starting capital over the test period.")
    return " ".join(bits)


def write_report(all_rows: list[dict], all_folds: dict, extra: dict, cfg, out: Path) -> Path:
    df = pd.DataFrame(all_rows)
    sel = cfg.selection
    lines = ["# Rotation strategy research (PAPER TRADING ONLY)", "",
             f"Generated {dt.datetime.now():%Y-%m-%d %H:%M}. Costs: {cfg.costs.describe()}. "
             f"Capital Rs {cfg.capital:,.0f}. Rebalance band {BAND_PCT:g}% of equity. Cash earns 0.", "",
             "**The bar** (same as the per-stock strategies): out-of-sample, net of costs, beat equal-weight "
             "buy-and-hold of the same universe on total return AND win at least "
             f"{sel.wf_min_fold_win_rate:.0%} of the {sel.wf_fold_months}-month walk-forward folds. "
             "All parameters were fixed in advance from published research / index rules; **no parameter was "
             "tuned, on either period**, and no variant was added after seeing results.", ""]
    for uni, info in extra.items():
        (is_s, is_e), (oos_s, oos_e) = info["periods"]["in_sample"], info["periods"]["out_of_sample"]
        b, ix = info["bench"]["bh"], info["bench"]["index"]
        lines += [f"## {uni} ({info['n_stocks']} stocks, today's constituents)", "",
                  f"In-sample {is_s.date()} to {is_e.date()}; out-of-sample {oos_s.date()} to {oos_e.date()}.", "",
                  "| Benchmark | IS CAGR | IS Sharpe | IS max DD | OOS CAGR | OOS Sharpe | OOS max DD | OOS total |",
                  "|---|---|---|---|---|---|---|---|"]
        for label, res in (("Equal-weight buy & hold (net of costs)", b), ("Nifty 50 index ^NSEI (price, no costs)", ix)):
            mi, mo = res["in_sample"].metrics, res["out_of_sample"].metrics
            lines.append(f"| {label} | {pct(mi['cagr'])} | {mi['sharpe']:.2f} | {pct(mi['max_drawdown'])} | "
                         f"{pct(mo['cagr'])} | {mo['sharpe']:.2f} | {pct(mo['max_drawdown'])} | "
                         f"{pct(mo['total_return'])} |")
        lines += ["", "| Strategy | IS CAGR | IS Sharpe | IS max DD | OOS CAGR | OOS Sharpe | OOS max DD | "
                      "OOS excess vs B&H | OOS excess vs Nifty | OOS folds won | Overfit? | Result |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in (x for x in all_rows if x["universe"] == uni):
            lines.append(
                f"| {r['label']} | {pct(r['is_cagr'])} | {r['is_sharpe']:.2f} | {pct(r['is_max_drawdown'])} | "
                f"{pct(r['oos_cagr'])} | {r['oos_sharpe']:.2f} | {pct(r['oos_max_drawdown'])} | "
                f"{pct(r['oos_excess_vs_bh'])} | {pct(r['oos_excess_vs_index'])} | "
                f"{round(r['oos_fold_win_rate'] * r['oos_folds'])}/{r['oos_folds']} | "
                f"{'yes' if r['overfit_flag'] else 'no'} | {'**PASS**' if r['passed'] else 'fail'} |")
        lines += ["", "Excess = difference in total return over the whole period (not per year).", "",
                  "### Verdict per candidate", ""]
        for r in (x for x in all_rows if x["universe"] == uni):
            lines.append(f"- **{r['label']}** (`{r['strategy']}`): {verdict(r, sel.wf_min_fold_win_rate)}")
        lines += ["", "### Out-of-sample folds (strategy vs buy-and-hold total return)", "",
                  "| Strategy | " + " | ".join(f"{f['start']:%b %Y}-{f['end']:%b %Y}"
                                               for f in next(iter(all_folds[uni].values()))) + " |",
                  "|---|" + "---|" * len(next(iter(all_folds[uni].values())))]
        for key, fl in all_folds[uni].items():
            lines.append(f"| {REGISTRY[key].label} | " + " | ".join(
                f"{pct(f['strategy_return'])} vs {pct(f['bh_return'])}{' W' if f['won'] else ''}" for f in fl) + " |")
        lines += ["", f"![equity](rotation_equity_{uni}.png)", ""]

    # survivorship estimate
    lines += ["## Caveats (read before trusting any PASS)", ""]
    surv = []
    for uni, info in extra.items():
        gaps = []
        for p, tag in (("in_sample", "in-sample"), ("out_of_sample", "out-of-sample")):
            b, ix = info["bench"]["bh"][p].metrics, info["bench"]["index"][p].metrics
            gaps.append(f"{pct(b['cagr'])}/yr vs {pct(ix['cagr'])}/yr {tag} (gap {pct(b['cagr'] - ix['cagr'])}/yr)")
        surv.append(f"{uni}: equal-weight buy-and-hold of today's members vs the Nifty 50 price index made "
                    + " and ".join(gaps))
    edge = {uni: pd.DataFrame([r for r in all_rows if r["universe"] == uni]).set_index("strategy")
            for uni in extra}
    if {"nifty50", "nifty100"} <= set(edge):
        diffs = (edge["nifty100"]["oos_cagr_excess_vs_bh"] - edge["nifty50"]["oos_cagr_excess_vs_bh"])
        surv.append("the out-of-sample CAGR edge over buy-and-hold is on average "
                    f"{pct(diffs.mean())}/yr larger on nifty100 than on nifty50 (momentum candidates: "
                    f"{pct(diffs[[k for k in diffs.index if 'momentum' in k and k != 'momentum_lowvol']].mean())}/yr)"
                    ", consistent with the extra mid-caps being hindsight winners")
    lines += [
        "1. **Survivorship bias (large).** The universe is TODAY's index members applied to the past. Stocks "
        "join the Nifty 50/100 *because* they rose, so the past looks better than it was, and momentum "
        "strategies (which buy what rose) benefit the most. Rough size: " + "; ".join(surv) + ". Part of that "
        "gap is dividends (~1-1.5%/yr, in the adjusted stock prices but not in the price index) and "
        "equal-weight vs cap-weight, the rest is hindsight. Compare the nifty50 and nifty100 tables: the wider, "
        "more mid-cap nifty100 list contains more 'future winners' that were small in 2015-2021.",
        "2. **Data quality.** yfinance split/dividend-adjusted closes; occasional bad ticks, missing days, and "
        "re-listed/merged companies (e.g. HDFC Bank after the HDFC merger, demergers such as Jio Financial) can "
        "distort a 12-month return. Stocks listed recently enter only after a year of history.",
        "3. **Few folds.** The out-of-sample period gives only ~5 yearly folds; a 3/5 score is weak evidence. "
        "A single regime (the 2023-24 Indian mid/large-cap rally, which strongly favoured momentum) dominates "
        "the test period.",
        f"4. **Costs.** {cfg.costs.describe()}; 0 brokerage (discount broker), 0.05% slippage per side, DP "
        "charge per sell. Real impact cost for a larger account, and STT changes, would raise them. Monthly "
        "rebalancing of top-10 momentum turns over ~3x a year, so costs matter.",
        "5. **Timing.** Decisions at the month's last close, fills at the next open; ignores the auction, "
        "circuit limits and partial fills. Cash earns 0 (a liquid fund would earn ~6-7%/yr, which would help "
        "the strategies that sit in cash).",
        "6. **A PASS is evidence, not proof.** Seven candidates were tested; with that many tries one can pass "
        "by luck. Paper-trade before trusting it.",
        ""]

    # recommendation: rule that does not use the test period to choose (pass on every universe tested, then best IN-SAMPLE Sharpe)
    unis = list(extra)
    by = df.groupby("strategy")
    passing = [k for k, g in by if g["passed"].all() and len(g) == len(unis) and not g["overfit_flag"].any()]
    lines += ["## Recommendation", ""]
    if passing:
        is_sharpe = df[df.strategy.isin(passing)].groupby("strategy")["is_sharpe"].mean().sort_values(ascending=False)
        pick = is_sharpe.index[0]
        lines += [f"Candidates passing on every universe tested ({', '.join(unis)}) with no overfit flag: "
                  f"{', '.join(f'`{k}`' for k in passing)}.", "",
                  f"Rule (chosen so the test period is not used to pick): among those, pick the best **in-sample** Sharpe, so the test "
                  f"period is only used as a pass/fail gate, not to choose the winner. That is **`{pick}`** "
                  f"({REGISTRY[pick].label}), in-sample Sharpe {is_sharpe.iloc[0]:.2f}.", "",
                  "Deploy for PAPER trading only, with the caveats above; re-run this report every quarter and "
                  "stop if it falls below buy-and-hold over a rolling 12 months by more than its costs."]
    else:
        lines += ["No candidate passed on every universe tested without an overfit flag. Recommendation: "
                  "**none** for real capital; the bot keeps holding cash (or simply buy-and-hold an index fund)."]
        some = [k for k, g in by if g["passed"].any()]
        if some:
            where = {k: ", ".join(by.get_group(k).loc[lambda g: g["passed"], "universe"]) for k in some}
            lines += ["", "Passed on only part of the universes: "
                      + "; ".join(f"`{k}` ({where[k]})" for k in some) + ". An edge that appears in the "
                      "wider, more survivorship-biased list but not in the Nifty 50 is exactly what hindsight "
                      "in the constituent list would produce, so treat it as unproven."]
    lines += ["", "_Process note: the seven candidates and their parameters were written down before any "
              "backtest was run; the recommendation rule above (pass everywhere, then best in-sample Sharpe) "
              "never uses out-of-sample numbers to choose between passers._"]
    path = out / "rotation_research.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--universe", nargs="+", default=["nifty100"], choices=["nifty50", "nifty100", "nifty500"])
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and re-download")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.log_dir)
    out = PROJECT_ROOT / "reports"
    out.mkdir(exist_ok=True)
    all_rows, all_folds, extra = [], {}, {}
    print("ROTATION RESEARCH - PAPER BACKTEST, NO REAL MONEY")
    for uni in args.universe:
        rows, folds, info = run_universe(uni, cfg, args.refresh)
        all_rows += rows
        all_folds[uni] = folds
        extra[uni] = info
        plot(uni, info["curves"], cfg.capital, out)
    pd.DataFrame(all_rows).to_csv(out / "rotation_research.csv", index=False)
    md = write_report(all_rows, all_folds, extra, cfg, out)
    print(f"\nWrote {md} and {out / 'rotation_research.csv'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
