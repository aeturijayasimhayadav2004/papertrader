"""Learn from results: do the agent's desks and chart setups actually help? PAPER TRADING ONLY.

Reads the agent's briefs and scan hits (Supabase when configured, plus local SQLite),
measures what each did afterwards, backfills the same questions from price history,
and writes plain-English suggestions. NOTHING is changed automatically: tuning rules on
the data that judges them overfits, so a person reads the report and decides.

Outputs:
  reports/learning.md                 the report in plain words
  reports/learning_outcomes.csv       every brief / scan hit x horizon
  reports/learning_scorecard.csv      all scorecard rows (agent_scorecard shape)
  reports/learning_setup_history.csv  scanner setups replayed on past days
  reports/learning_odds_trades.csv    every past trade with the odds desk's verdict at the time

Examples:
  python scripts/learn.py                         # Nifty 50, 5 years of history
  python scripts/learn.py --universe config --history-years 3
  python scripts/learn.py --no-history            # live briefs / scans only (fast)
  python scripts/learn.py --write-db              # also save agent_outcomes / agent_scorecard
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from papertrader.agent import learning as L  # noqa: E402
from papertrader.config import load_config  # noqa: E402

log = logging.getLogger("learn")
REPORTS = ROOT / "reports"


def _universe(cfg, name: str) -> list[str]:
    if name == "config":
        return list(cfg.universe)
    from papertrader.agent.universe import load_universe
    return load_universe(name, cache_dir=cfg.data.cache_dir / "universe")


def _fmt(x, pct: bool = True) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "-"
    return f"{x:+.2f}%" if pct else f"{x:.0%}"


def _table(rows: list[dict], cols: list[tuple[str, str, str]]) -> str:
    """Markdown table; cols = (header, key, kind) with kind pct | rate | raw."""
    head = "| " + " | ".join(c[0] for c in cols) + " |\n|" + "---|" * len(cols) + "\n"
    body = []
    for r in rows:
        cells = []
        for _, k, kind in cols:
            v = r.get(k)
            cells.append(_fmt(v) if kind == "pct" else _fmt(v, False) if kind == "rate" else ("-" if v is None else str(v)))
        body.append("| " + " | ".join(cells) + " |")
    return head + "\n".join(body) + "\n"


def _write_db(store, outcomes: list[dict], scorecard: list[dict]) -> str:
    from papertrader.storage.schema import TABLES, upsert

    if "agent_outcomes" not in TABLES or "agent_scorecard" not in TABLES:
        return "skipped: agent_outcomes / agent_scorecard are not in storage/schema.py yet"
    ops = [upsert("agent_outcomes", L.outcome_rows(outcomes)), upsert("agent_scorecard", scorecard)]
    client = getattr(store.remote, "client", None)
    if client is not None:
        try:   # check the tables exist remotely, so a missing table never jams the sync outbox
            for t in ("agent_outcomes", "agent_scorecard"):
                client.table(t).select("id").limit(1).execute()
        except Exception as exc:
            store.sqlite.apply(ops, queue_for_sync=False)
            return f"local SQLite only (Supabase tables not ready: {str(exc)[:120]})"
        store.write(ops)
        err = store.sync()
        return "Supabase + local SQLite" + (f" (sync error: {err})" if err else "")
    store.sqlite.apply(ops, queue_for_sync=False)
    return "local SQLite only"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--history-years", type=float, default=5)
    ap.add_argument("--universe", default="nifty50", choices=["nifty50", "nifty100", "nifty500", "config"])
    ap.add_argument("--no-history", action="store_true", help="skip the history backfills")
    ap.add_argument("--no-odds", action="store_true", help="skip only the odds-desk backfill")
    ap.add_argument("--odds-gap-days", type=int, default=0,
                    help="evaluate at most one trade per stock and strategy in this many days (0 = all)")
    ap.add_argument("--write-db", action="store_true", help="save agent_outcomes / agent_scorecard rows")
    ap.add_argument("--out", default=str(REPORTS))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    t_all = time.perf_counter()

    cfg = load_config()
    from papertrader.data import load_prices
    from papertrader.storage import Store

    store = Store.from_config(cfg)
    briefs = L.read_rows(store, "agent_briefs")
    scans = L.read_rows(store, "agent_scans")
    print(f"Read {len(briefs)} brief(s) and {len(scans)} scan hit(s).")

    today = dt.date.today()
    hist_start = today - dt.timedelta(days=int(365.25 * args.history_years))
    tickers = [] if args.no_history else _universe(cfg, args.universe)
    tickers = list(dict.fromkeys(tickers + [b["ticker"] for b in briefs + scans if b.get("ticker")]))
    # warm-up before the history window: the odds desk looks back odds_lookback_days, setups need a year
    warm = max(cfg.agent.odds_lookback_days, 400) + 30
    data_start = min(hist_start - dt.timedelta(days=warm),
                     min((L._date(b["as_of_date"]) for b in briefs + scans), default=today) - dt.timedelta(days=400))
    data_cfg = dataclasses.replace(cfg.data, start=data_start, cache_dir=cfg.data.cache_dir / "learn")
    t0 = time.perf_counter()
    prices, warnings = load_prices(tickers + [cfg.benchmark], data_cfg)
    bench = prices.pop(cfg.benchmark, None)
    print(f"Prices: {len(prices)} stock(s) from {data_cfg.start} ({len(warnings)} warning(s)) "
          f"in {time.perf_counter() - t0:.0f}s.")

    hs = L.HORIZONS
    outcomes = L.brief_outcomes(briefs, prices, bench, hs) + L.scan_outcomes(scans, prices, bench, hs)
    desk_rows = L.desk_scorecard(outcomes)
    setup_rows = L.setup_scorecard(outcomes)

    history_rows: list[dict] = []
    odds_eval = None
    if not args.no_history:
        uni = {t: prices[t] for t in _universe(cfg, args.universe) if t in prices}
        t0 = time.perf_counter()
        history_rows = L.historical_setup_scorecard(uni, bench, hist_start, today, L.HISTORY_HORIZONS)
        t_setups = time.perf_counter() - t0
        print(f"Setup history: {len(uni)} stocks, {hist_start} to {today}, {t_setups:.1f}s.")
        if not args.no_odds:
            from papertrader.backtest import market_regime
            from papertrader.strategies import build_strategies

            regime = market_regime(bench, cfg.regime.sma_window) if cfg.regime.enabled else None
            odds_eval = L.historical_odds_evaluation(uni, build_strategies(cfg.strategies), cfg.risk.exit_rules(),
                                                     cfg.costs.model(), cfg.agent, hist_start, today,
                                                     regime=regime, min_gap_days=args.odds_gap_days)
            print(f"Odds history: {odds_eval['groups']['all']['n']} trades in {odds_eval['runtime_s']:.0f}s.")

    tips = L.suggestions(desk_rows, setup_rows, odds_eval, history_rows)
    scorecard = L.scorecard_rows(desk_rows, setup_rows, history_rows, odds_eval, as_of=today)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(outcomes).to_csv(out / "learning_outcomes.csv", index=False)
    pd.DataFrame(scorecard).to_csv(out / "learning_scorecard.csv", index=False)
    if history_rows:
        pd.DataFrame(history_rows).to_csv(out / "learning_setup_history.csv", index=False)
    if odds_eval:
        pd.DataFrame(odds_eval["trades"]).to_csv(out / "learning_odds_trades.csv", index=False)

    md = [f"# What the agent has learned ({today})", "",
          "PAPER TRADING ONLY. Measurements and suggestions; nothing here changes the bot. "
          f"Groups with fewer than {L.MIN_N} results are marked too few to judge.", "",
          "## Suggestions", ""] + [f"- {t}" for t in tips] + [""]
    done_briefs = len({r["brief_id"] for r in outcomes if r["kind"] == "brief" and r["status"] == "done"})
    md += ["## Live briefs and scan hits", "",
           f"{len(briefs)} brief(s), {done_briefs} with at least one finished horizon; {len(scans)} scan hit(s). "
           "Entry = next trading day's open; horizon h = close h trading days after the signal day.", ""]
    live_cols = [("kind", "kind", "raw"), ("key", "key", "raw"), ("status", "status", "raw"),
                 ("days", "horizon_days", "raw"), ("n", "n", "raw"), ("mean", "mean_ret_pct", "pct"),
                 ("median", "median_ret_pct", "pct"), ("hit rate", "hit_rate", "rate"),
                 ("excess vs Nifty", "mean_excess_pct", "pct"), ("note", "note", "raw")]
    if desk_rows or setup_rows:
        md += [_table(desk_rows + setup_rows, live_cols)]
    if history_rows:
        md += ["## Scanner setups on past days", "",
               f"{hist_start} to {today}, {args.universe}. Excess = setup's forward return minus the average of "
               "every stock in the universe over the same window. Neighbouring days' windows overlap, so the "
               "real sample is smaller than n. Today's index members only (survivorship bias).", "",
               _table(sorted(history_rows, key=lambda r: (r["horizon_days"], -(r["mean_excess_pct"] or 0))),
                      [("setup", "key", "raw"), ("days", "horizon_days", "raw"), ("n", "n", "raw"),
                       ("stocks", "tickers", "raw"), ("mean", "mean_ret_pct", "pct"),
                       ("median", "median_ret_pct", "pct"), ("hit rate", "hit_rate", "rate"),
                       ("excess", "mean_excess_pct", "pct"), ("beat universe", "excess_hit_rate", "rate"),
                       ("years +", "years_positive", "raw")])]
    if odds_eval:
        g = odds_eval["groups"]
        p = odds_eval["params"]
        md += ["## Would the odds desk have helped?", "",
               f"Every past BUY trade of the {len(odds_eval['strategies'])} enabled strategies on "
               f"{odds_eval['tickers']} stocks, {odds_eval['start']} to {odds_eval['end']} (same exits and costs as "
               f"the bot{', market regime filter on' if cfg.regime.enabled else ''}). For each, the real odds desk "
               f"ran on bars up to the signal day only (min {p['odds_min_signals']} signals, min win rate "
               f"{p['odds_min_win_rate']:.0%}, {p['odds_lookback_days']}-day lookback). "
               f"{odds_eval['open_at_end']} trade(s) still open at the end are left out.", "",
               _table([{"group": k, **v} for k, v in g.items()],
                      [("group", "group", "raw"), ("trades", "n", "raw"), ("won", "win_rate", "rate"),
                       ("mean", "mean_ret_pct", "pct"), ("median", "median_ret_pct", "pct")]), "",
               "By strategy:", "",
               _table([{"strategy": s, "group": k, **v} for s, gg in odds_eval["by_strategy"].items()
                       for k, v in gg.items()],
                      [("strategy", "strategy", "raw"), ("group", "group", "raw"), ("trades", "n", "raw"),
                       ("won", "win_rate", "rate"), ("mean", "mean_ret_pct", "pct")])]
    (out / "learning.md").write_text("\n".join(md), encoding="utf-8")

    if args.write_db:
        print("Database:", _write_db(store, outcomes, scorecard))

    print("\nSuggestions (nothing applied):")
    for t in tips:
        print(" -", t)
    print(f"\nWrote {out / 'learning.md'} and CSVs in {time.perf_counter() - t_all:.0f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
