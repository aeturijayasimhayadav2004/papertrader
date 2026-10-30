"""Learning desk: did the agent's desks and chart setups actually help? PAPER TRADING ONLY.

This module only MEASURES and SUGGESTS. It never changes config.yaml: tuning the
rules on the same data we judge them by would overfit, so the output is a set of
scorecards plus plain-English suggestions, each with its sample size.

Forward returns (how a brief or a scan hit did afterwards)
  The bot fills at the NEXT trading day's open after the signal day, so the entry is
  that open. Horizon h ends at the close h trading days after the signal day
  (h = 1 is the entry day's own close). Also reported: the excess over the benchmark
  for the same window (benchmark open on the entry day to its close on the exit day)
  and the maximum adverse excursion (lowest low from entry to exit, vs the entry).
  A horizon that has not finished yet is "pending" and has no numbers.

Live scorecards (from the agent_briefs / agent_scans tables)
  Per desk x status, per verdict, per brain decision and per setup, at each horizon:
  n, mean, median, hit rate (share that went up), mean excess. Rows for BLOCK
  verdicts, desk "block" findings and veto decisions also get an "avoided loss rate"
  (share that went down), compared with the same rate over all briefs. Fewer than
  MIN_N (20) results is flagged "too few to judge".

History backfills (useful from day one)
  historical_setup_scorecard  replays the scanner's daily setup rules on every past
                              day (vectorised per stock; a test checks the flags equal
                              scanner.scan_daily on sampled days) and compares each
                              setup's forward return with the average forward return of
                              the whole universe on the same days.
  historical_odds_evaluation  takes every past BUY trade of each strategy on each stock
                              (backtest.simulate_sleeve) and asks the real odds desk
                              (timemachine.odds_check) what it would have said using only
                              bars up to that trade's signal day. Then it compares the
                              realised returns of the trades the desk would have BLOCKED
                              with the ones it would have ALLOWED.

Caveats: forward windows of neighbouring days overlap (a setup that fires five days in a
row is five rows that share most of their future), so the true sample is smaller than n.
Survivorship: the universe is today's index list, which leaves out stocks that dropped out.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np
import pandas as pd

from papertrader import indicators as ind
from papertrader.agent import scanner as sc
from papertrader.agent import timemachine
from papertrader.agent.types import BLOCK, UNKNOWN

log = logging.getLogger(__name__)

HORIZONS = (1, 5, 20)
HISTORY_HORIZONS = (5, 20)
MIN_N = 20                                  # below this a group is "too few to judge"
TOO_FEW = f"too few to judge (n < {MIN_N})"
VETO_DECISIONS = {"veto", "skip", "block", "reject", "no", "pass", "avoid", "sell"}
EDGE_PCT = 0.5                              # a setup's mean excess must beat +/- this (points) to be called an edge
OUTCOME_KEYS = ("id", "brief_id", "kind", "as_of_date", "ticker", "strategy", "verdict", "horizon_days",
                "ret_pct", "excess_pct", "mae_pct", "status", "desks", "updated_at")
SCORECARD_KEYS = ("id", "as_of_date", "kind", "key", "status", "horizon_days", "n", "mean_ret_pct",
                  "median_ret_pct", "hit_rate", "mean_excess_pct", "note")


@dataclass(frozen=True)
class LearningOptions:
    """The `learning:` section of config.yaml (all optional)."""
    horizons: tuple[int, ...] = HORIZONS
    history_horizons: tuple[int, ...] = HISTORY_HORIZONS
    history_years: int = 5
    min_n: int = MIN_N

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "LearningOptions":
        raw = raw or {}
        return cls(horizons=tuple(int(h) for h in raw.get("horizons", HORIZONS)),
                   history_horizons=tuple(int(h) for h in raw.get("history_horizons", HISTORY_HORIZONS)),
                   history_years=int(raw.get("history_years", 5)),
                   min_n=int(raw.get("min_n", MIN_N)))


# ---------------------------------------------------------------- small helpers

def _date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    return dt.date.fromisoformat(str(value)[:10])


def _r(x: float | None, nd: int = 3) -> float | None:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), nd)


def _findings(brief: dict[str, Any]) -> list[dict[str, Any]]:
    f = brief.get("findings") or []
    if isinstance(f, str):
        try:
            f = json.loads(f)
        except ValueError:
            return []
    return [x for x in f if isinstance(x, dict)]


def _brain_decision(finding: dict[str, Any]) -> str | None:
    data = finding.get("data") or {}
    if isinstance(data, dict):
        for k in ("decision", "action", "verdict"):
            if data.get(k):
                return str(data[k]).lower()
    return None


def _window_return(df: pd.DataFrame, entry_day: pd.Timestamp, exit_day: pd.Timestamp) -> float | None:
    """Open on entry_day (or the first bar after it) to the close on exit_day (or the last bar before)."""
    w = df.loc[entry_day:exit_day]
    if w.empty:
        return None
    return float(w["Close"].iloc[-1]) / float(w["Open"].iloc[0]) - 1.0


# ---------------------------------------------------------------- forward returns

def forward_returns(prices: dict[str, pd.DataFrame], ticker: str, as_of: dt.date,
                    horizons: Iterable[int] = HORIZONS, benchmark: pd.DataFrame | None = None) -> dict[str, Any]:
    """What a buy on `as_of`'s signal did afterwards (see the module doc for the timing).

    Returns {"ticker", "as_of_date", "entry_date", "entry_price", "horizons": {h: {...}}},
    where each horizon has status "done" (ret_pct, excess_pct, mae_pct, exit_date),
    "pending" (not reached yet; numbers None) or "no_data" (no prices for the stock).
    """
    as_of = _date(as_of)
    hs = [int(h) for h in horizons]
    out: dict[str, Any] = {"ticker": ticker, "as_of_date": as_of, "entry_date": None, "entry_price": None,
                           "horizons": {}}
    empty = {"ret_pct": None, "excess_pct": None, "mae_pct": None, "exit_date": None}
    df = (prices or {}).get(ticker)
    if df is None or df.empty:
        out["horizons"] = {h: {**empty, "status": "no_data"} for h in hs}
        return out
    df = df.sort_index()
    i0 = int(df.index.searchsorted(pd.Timestamp(as_of), side="right"))   # first bar after the signal day
    if i0 >= len(df):
        out["horizons"] = {h: {**empty, "status": "pending"} for h in hs}
        return out
    entry = float(df["Open"].iloc[i0])
    out["entry_date"], out["entry_price"] = df.index[i0].date(), round(entry, 4)
    lows = df["Low"].to_numpy(float)
    for h in hs:
        j = i0 + h - 1
        if h < 1 or j >= len(df):
            out["horizons"][h] = {**empty, "status": "pending"}
            continue
        ret = float(df["Close"].iloc[j]) / entry - 1.0
        mae = min(0.0, float(lows[i0:j + 1].min()) / entry - 1.0)
        excess = None
        if benchmark is not None and not benchmark.empty:
            b = _window_return(benchmark.sort_index(), df.index[i0], df.index[j])
            excess = None if b is None else ret - b
        out["horizons"][h] = {"status": "done", "ret_pct": _r(100 * ret),
                              "excess_pct": _r(100 * excess) if excess is not None else None,
                              "mae_pct": _r(100 * mae), "exit_date": df.index[j].date()}
    return out


def brief_outcomes(briefs: list[dict[str, Any]], prices: dict[str, pd.DataFrame],
                   benchmark: pd.DataFrame | None = None, horizons: Iterable[int] = HORIZONS) -> list[dict[str, Any]]:
    """One row per brief per horizon, with every desk's status flattened (events_status, ...)."""
    hs = tuple(horizons)
    rows: list[dict[str, Any]] = []
    for b in briefs or []:
        try:
            day = _date(b["as_of_date"])
        except (KeyError, ValueError):
            log.warning("Brief without a usable date skipped: %s", b.get("id"))
            continue
        ticker, strategy = b.get("ticker"), b.get("strategy")
        bid = b.get("id") or f"{day}|{ticker}|{strategy}"
        desks: dict[str, Any] = {}
        for f in _findings(b):
            desk = str(f.get("desk") or "")
            if not desk:
                continue
            desks[f"{desk}_status"] = f.get("status")
            if desk == "brain":
                desks["brain_decision"] = _brain_decision(f)
        fr = forward_returns(prices, ticker, day, hs, benchmark)
        for h in hs:
            r = fr["horizons"][h]
            rows.append({"id": f"{bid}|{h}", "brief_id": bid, "kind": "brief", "as_of_date": day,
                         "ticker": ticker, "strategy": strategy, "verdict": b.get("verdict"), "horizon_days": h,
                         "ret_pct": r["ret_pct"], "excess_pct": r["excess_pct"], "mae_pct": r["mae_pct"],
                         "status": r["status"], **desks})
    return rows


def scan_outcomes(scans: list[dict[str, Any]], prices: dict[str, pd.DataFrame],
                  benchmark: pd.DataFrame | None = None, horizons: Iterable[int] = HORIZONS) -> list[dict[str, Any]]:
    """One row per scan hit per horizon (the setup goes in `setup` and in `strategy`)."""
    hs = tuple(horizons)
    rows: list[dict[str, Any]] = []
    for s in scans or []:
        try:
            day = _date(s["as_of_date"])
        except (KeyError, ValueError):
            continue
        ticker, setup = s.get("ticker"), s.get("setup")
        sid = s.get("id") or f"{day}|{ticker}|{setup}"
        fr = forward_returns(prices, ticker, day, hs, benchmark)
        for h in hs:
            r = fr["horizons"][h]
            rows.append({"id": f"{sid}|{h}", "brief_id": sid, "kind": "scan", "as_of_date": day,
                         "ticker": ticker, "strategy": setup, "setup": setup, "verdict": None,
                         "score": s.get("score"), "horizon_days": h, "ret_pct": r["ret_pct"],
                         "excess_pct": r["excess_pct"], "mae_pct": r["mae_pct"], "status": r["status"]})
    return rows


# ---------------------------------------------------------------- scorecards

def _stats(rets: list[float], excess: list[float | None]) -> dict[str, Any]:
    a = np.asarray(rets, dtype=float)
    ex = np.asarray([e for e in excess if e is not None], dtype=float)
    n = len(a)
    return {"n": n,
            "mean_ret_pct": _r(a.mean()) if n else None,
            "median_ret_pct": _r(float(np.median(a))) if n else None,
            "hit_rate": _r(float((a > 0).mean())) if n else None,
            "loss_rate": _r(float((a < 0).mean())) if n else None,
            "mean_excess_pct": _r(ex.mean()) if len(ex) else None,
            "enough": n >= MIN_N}


def _group(rows: list[dict[str, Any]], kind: str, key: str, status_of, vetoish) -> list[dict[str, Any]]:
    """Aggregate done rows by (status_of(row), horizon)."""
    base: dict[int, list[dict]] = {}
    groups: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        base.setdefault(r["horizon_days"], []).append(r)
        st = status_of(r)
        if st is None:
            continue
        groups.setdefault((str(st), r["horizon_days"]), []).append(r)
    out = []
    for (st, h), rs in sorted(groups.items()):
        s = _stats([r["ret_pct"] for r in rs], [r.get("excess_pct") for r in rs])
        b = _stats([r["ret_pct"] for r in base[h]], [r.get("excess_pct") for r in base[h]])
        row = {"kind": kind, "key": key, "status": st, "horizon_days": h, **s,
               "baseline_n": b["n"], "baseline_mean_ret_pct": b["mean_ret_pct"],
               "baseline_loss_rate": b["loss_rate"], "avoided_loss_rate": None}
        if vetoish(st):
            row["avoided_loss_rate"] = s["loss_rate"]
        row["note"] = _note(row)
        out.append(row)
    return out


def _note(row: dict[str, Any]) -> str:
    parts = []
    if row["n"] < MIN_N:
        parts.append(TOO_FEW)
    if row.get("avoided_loss_rate") is not None:
        parts.append(f"avoided loss rate {row['avoided_loss_rate']:.0%} vs {row['baseline_loss_rate']:.0%} "
                     f"for all (n={row['baseline_n']})")
    if row.get("extra_note"):
        parts.append(row["extra_note"])
    return "; ".join(parts)


def _done(outcomes: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [r for r in outcomes or [] if r.get("kind", kind) == kind and r.get("status") == "done"
            and r.get("ret_pct") is not None]


def desk_scorecard(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per desk x status, per verdict (plus "all" as the baseline) and per brain decision, at each horizon."""
    done = _done(outcomes, "brief")
    out: list[dict[str, Any]] = []
    out += _group(done, "verdict", "all", lambda r: "all", lambda s: False)
    out += _group(done, "verdict", "verdict", lambda r: r.get("verdict"), lambda s: s == "BLOCK")
    desks = sorted({k[:-7] for r in done for k in r if k.endswith("_status") and k != "status"})
    for d in desks:
        out += _group(done, "desk", d, lambda r, d=d: r.get(f"{d}_status"), lambda s: s == BLOCK)
    if any(r.get("brain_decision") for r in done):
        out += _group(done, "brain", "brain_decision", lambda r: r.get("brain_decision"),
                      lambda s: s in VETO_DECISIONS)
    return out


def setup_scorecard(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per setup type at each horizon, from the live scan hits."""
    done = _done(outcomes, "scan")
    out = _group(done, "setup", "all", lambda r: "all", lambda s: False)
    for setup in sorted({r.get("setup") or r.get("strategy") for r in done} - {None}):
        out += _group([r for r in done if (r.get("setup") or r.get("strategy")) == setup], "setup", setup,
                      lambda r: "all", lambda s: False)
    # the per-setup groups above used their own rows as the baseline: point them at all scan hits instead
    allrows = {r["horizon_days"]: r for r in out if r["key"] == "all"}
    for r in out:
        a = allrows.get(r["horizon_days"])
        if a is not None:
            r["baseline_n"], r["baseline_mean_ret_pct"] = a["n"], a["mean_ret_pct"]
    return out


# ---------------------------------------------------------------- history: scanner setups

def setup_flags(df: pd.DataFrame) -> pd.DataFrame:
    """The scanner's daily setup rules on EVERY bar at once (bool frame, one column per setup).

    Bar t's flags use bars <= t only, and equal scanner.scan_daily on df.loc[:t] (tested).
    """
    d = sc._clean_daily(df)
    if d is None:
        return pd.DataFrame(columns=list(sc.SETUPS), dtype=bool)
    c, o, h, v = d["Close"], d["Open"], d["High"], d["Volume"]
    prev = c.shift(1)
    r = ind.rsi(c, 14)
    ma20 = ind.sma(c, 20)
    lb = sc.BREAKOUT_LOOKBACK
    hi20 = h.rolling(lb, min_periods=lb).max().shift(1)
    avg_v = v.rolling(lb, min_periods=lb).mean().shift(1)
    vx = (v / avg_v).where(avg_v > 0, 0.0)
    run = 100.0 * (c.shift(5) / c.shift(15) - 1.0)
    rng5 = 100.0 * (c.rolling(5).max() - c.rolling(5).min()) / c
    recent_min = r.shift(1).rolling(3, min_periods=1).min()
    gap = 100.0 * (o / prev - 1.0)
    hi52 = h.rolling(sc.YEAR, min_periods=1).max()
    dist = 100.0 * (1.0 - c / hi52)
    f = pd.DataFrame({
        "breakout": (c > hi20) & (vx >= sc.BREAKOUT_VOLUME_X),
        "bull_flag": (run > sc.FLAG_RUN_PCT) & (rng5 < sc.FLAG_RANGE_PCT) & ma20.notna() & (c > ma20),
        "reclaim_ma20": ma20.notna() & ma20.shift(1).notna() & (prev < ma20.shift(1)) & (c > ma20),
        "oversold": r.notna() & (r < 30),
        "oversold_bounce": r.notna() & recent_min.notna() & (recent_min < 30) & (c > prev),
        "gap_up": gap >= sc.GAP_PCT,
        "gap_down": gap <= -sc.GAP_PCT,
        "near_52w_high": dist <= sc.NEAR_HIGH_PCT,
    }, index=d.index).fillna(False).astype(bool)
    f.iloc[:sc.MIN_BARS - 1] = False             # the scanner needs MIN_BARS bars before it looks at a stock
    return f


def _fwd(d: pd.DataFrame, h: int) -> pd.Series:
    """Percent return from the next bar's open to the close h bars after this one."""
    return 100.0 * (d["Close"].shift(-h) / d["Open"].shift(-1) - 1.0)


def historical_setup_scorecard(prices: dict[str, pd.DataFrame], benchmark: pd.DataFrame | None,
                               start: dt.date, end: dt.date, horizons: Iterable[int] = HISTORY_HORIZONS,
                               step_days: int = 1) -> list[dict[str, Any]]:
    """Replay the scanner's setups on past days and score their forward returns.

    Excess = the setup's forward return minus the AVERAGE forward return of every stock in
    `prices` over the same window (so a rising market does not flatter a setup).
    Also returns key "all" (every stock-day) and key "benchmark" (the index itself, excess = the
    stock-day average minus the index). Days are sampled every `step_days` trading days.
    """
    hs = [int(h) for h in horizons]
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    long_rows: list[pd.DataFrame] = []
    for ticker, raw in (prices or {}).items():
        try:
            d = sc._clean_daily(raw)
            if d is None:
                continue
            flags = setup_flags(d)
            frame = flags.copy()
            for h in hs:
                frame[f"fwd_{h}"] = _fwd(d, h)
            frame = frame.loc[start_ts:end_ts]
            frame["ticker"] = ticker
            long_rows.append(frame)
        except Exception as exc:
            log.warning("Setup history skipped %s: %s", ticker, exc)
    if not long_rows:
        return []
    allf = pd.concat(long_rows)
    allf.index.name = "Date"
    days = allf.index.unique().sort_values()
    if step_days > 1:
        allf = allf[allf.index.isin(days[::step_days])]
    out: list[dict[str, Any]] = []
    for h in hs:
        col = f"fwd_{h}"
        sub = allf[allf[col].notna()]
        if sub.empty:
            continue
        uni = sub.groupby(level=0)[col].mean()          # universe average forward return per day
        excess = sub[col] - uni.reindex(sub.index).to_numpy()
        years = sub.index.year

        def row(key: str, mask: pd.Series | np.ndarray, extra: str = "") -> dict[str, Any]:
            rets = sub[col][mask]
            ex = excess[mask]
            s = _stats(rets.tolist(), ex.tolist())
            yr = ex.groupby(years[np.asarray(mask)]).mean()
            yrs_pos = int((yr > 0).sum())
            r = {"kind": "setup_history", "key": key, "status": "all", "horizon_days": h, **s,
                 "tickers": int(sub["ticker"][mask].nunique()), "days": int(sub.index[np.asarray(mask)].nunique()),
                 "years_positive": f"{yrs_pos}/{len(yr)}", "excess_hit_rate": _r(float((ex > 0).mean())) if len(ex) else None}
            r["note"] = "; ".join(p for p in (TOO_FEW if s["n"] < MIN_N else "",
                                              f"beat the universe in {yrs_pos}/{len(yr)} years" if key != "all" else "",
                                              extra) if p)
            return r

        out.append(row("all", np.ones(len(sub), dtype=bool), "every stock-day: the baseline"))
        for setup in sc.SETUPS:
            m = sub[setup].to_numpy(bool)
            if m.any():
                out.append(row(setup, m))
        if benchmark is not None and not benchmark.empty:
            bd = benchmark.sort_index()
            bf = _fwd(bd, h).reindex(uni.index)
            ok = bf.notna()
            if ok.any():
                s = _stats(bf[ok].tolist(), (uni[ok] - bf[ok]).tolist())
                out.append({"kind": "setup_history", "key": "benchmark", "status": "all", "horizon_days": h, **s,
                            "tickers": 1, "days": int(ok.sum()), "years_positive": "", "excess_hit_rate": None,
                            "note": "the index itself; excess = universe average minus the index"})
    return out


# ---------------------------------------------------------------- history: the odds desk

def _trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    a = np.asarray([t["return_pct"] for t in trades], dtype=float)
    n = len(a)
    return {"n": n, "win_rate": _r(float((a > 0).mean())) if n else None,
            "mean_ret_pct": _r(a.mean()) if n else None,
            "median_ret_pct": _r(float(np.median(a))) if n else None,
            "sum_ret_pct": _r(a.sum()) if n else None, "enough": n >= MIN_N}


def historical_odds_evaluation(prices: dict[str, pd.DataFrame], strategies: list, exits, costs, cfg_agent,
                               start: dt.date, end: dt.date, regime: pd.Series | None = None,
                               min_gap_days: int = 0) -> dict[str, Any]:
    """Would the odds desk have helped? Every past BUY trade between start and end, judged by
    timemachine.odds_check on the bars up to (and including) that trade's signal day only.

    groups: "blocked" (desk said BLOCK), "allowed" (enough past signals, not blocked),
    "too_few" (under odds_min_signals past signals: never blocked), "unknown" (desk failed).
    Trades still open at `end` have no result and are left out. `min_gap_days` > 0 evaluates
    at most one trade per stock and strategy in that many calendar days (to save time).
    """
    from papertrader.backtest import simulate_sleeve

    t0 = time.perf_counter()
    trades: list[dict[str, Any]] = []
    open_at_end = 0
    for ticker, df in (prices or {}).items():
        if df is None or df.empty:
            continue
        df = df.sort_index().loc[:pd.Timestamp(end)]
        for strat in strategies:
            try:
                sleeve = simulate_sleeve(df, strat.signals(df), start, end, 1_000_000.0, costs, ticker=ticker,
                                         exits=exits, regime=regime)
            except Exception as exc:
                log.warning("Odds history: %s %s failed: %s", ticker, strat.name, exc)
                continue
            last_eval: pd.Timestamp | None = None
            for t in sleeve.trades:
                if t.exit_reason == "end_of_period":
                    open_at_end += 1
                    continue
                if min_gap_days and last_eval is not None and (t.signal_date - last_eval).days < min_gap_days:
                    continue
                last_eval = t.signal_date
                past = df.loc[:t.signal_date]                 # never a bar after the signal day
                f = timemachine.odds_check(past, strat, exits, costs, lookback_days=cfg_agent.odds_lookback_days,
                                           min_signals=cfg_agent.odds_min_signals,
                                           min_win_rate=cfg_agent.odds_min_win_rate, ticker=ticker)
                n_past = (f.data or {}).get("n_trades")
                group = ("blocked" if f.status == BLOCK else "unknown" if f.status == UNKNOWN
                         else "too_few" if (n_past or 0) < cfg_agent.odds_min_signals else "allowed")
                trades.append({"ticker": ticker, "strategy": strat.name,
                               "signal_date": t.signal_date.date(), "entry_date": t.entry_date.date(),
                               "exit_date": t.exit_date.date(), "return_pct": round(100 * t.return_pct, 3),
                               "exit_reason": t.exit_reason, "odds_status": f.status, "group": group,
                               "past_n": n_past, "past_win_rate": (f.data or {}).get("win_rate"),
                               "past_avg_pct": (f.data or {}).get("avg_return_pct"),
                               "odds_data_to": (f.data or {}).get("to")})
    groups = {g: _trade_stats([t for t in trades if t["group"] == g])
              for g in ("blocked", "allowed", "too_few", "unknown")}
    groups["not_blocked"] = _trade_stats([t for t in trades if t["group"] != "blocked"])
    groups["all"] = _trade_stats(trades)
    by_strategy = {}
    for name in sorted({t["strategy"] for t in trades}):
        ts = [t for t in trades if t["strategy"] == name]
        by_strategy[name] = {g: _trade_stats([t for t in ts if t["group"] == g])
                             for g in ("blocked", "allowed", "too_few")}
    return {"start": _date(start), "end": _date(end), "tickers": len(prices or {}),
            "strategies": [s.name for s in strategies], "groups": groups, "by_strategy": by_strategy,
            "trades": trades, "open_at_end": open_at_end, "min_gap_days": min_gap_days,
            "params": {"odds_min_signals": cfg_agent.odds_min_signals,
                       "odds_min_win_rate": cfg_agent.odds_min_win_rate,
                       "odds_lookback_days": cfg_agent.odds_lookback_days},
            "runtime_s": round(time.perf_counter() - t0, 1)}


# ---------------------------------------------------------------- suggestions

def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.2f}%"


def suggestions(desk_rows: list[dict[str, Any]] | None, setup_rows: list[dict[str, Any]] | None,
                odds_eval: dict[str, Any] | None, history_rows: list[dict[str, Any]] | None = None) -> list[str]:
    """Plain-English suggestions, each with its evidence and sample size. Nothing is applied:
    a person reads these and decides. Nothing is recommended on fewer than MIN_N results."""
    out: list[str] = []
    desk_rows = desk_rows or []

    # live briefs
    base = [r for r in desk_rows if r["kind"] == "verdict" and r["key"] == "all"]
    longest = max(base, key=lambda r: r["horizon_days"]) if base else None
    if not base or all(r["n"] < MIN_N for r in base):
        n = max((r["n"] for r in base), default=0)
        out.append(f"Too few finished briefs yet (n={n}, need {MIN_N}): keep collecting before judging the "
                   f"live desks.")
    else:
        h = longest["horizon_days"]
        for r in desk_rows:
            if r["horizon_days"] != h or r["kind"] not in ("desk", "verdict", "brain") or r["key"] == "all":
                continue
            who = f"{r['key']} desk '{r['status']}'" if r["kind"] == "desk" else f"{r['key']} {r['status']}"
            if r["n"] < MIN_N:
                continue
            if r.get("avoided_loss_rate") is not None:
                good = r["avoided_loss_rate"] > (longest["loss_rate"] or 0)
                out.append(f"{who}: {r['avoided_loss_rate']:.0%} of the {r['n']} stocks it stopped fell over {h} days "
                           f"(vs {longest['loss_rate']:.0%} of all {longest['n']} briefs), average "
                           f"{_pct(r['mean_ret_pct'])} vs {_pct(longest['mean_ret_pct'])}: "
                           + ("it is avoiding losers, keep it." if good else
                              "it stops winners about as often as losers; worth a look."))
            elif r["kind"] == "desk" and r["status"] == "warn":
                out.append(f"{who}: {r['n']} warned buys averaged {_pct(r['mean_ret_pct'])} over {h} days vs "
                           f"{_pct(longest['mean_ret_pct'])} for all briefs.")

    # live scan setups
    live = [r for r in setup_rows or [] if r["kind"] == "setup" and r["key"] != "all"]
    if setup_rows is not None and not live:
        out.append("No finished live scan hits yet: keep collecting; use the history backfill below for now.")
    elif live and all(r["n"] < MIN_N for r in live):
        n = max(r["n"] for r in live)
        out.append(f"Too few finished live scan hits per setup yet (largest n={n}): keep collecting; "
                   f"use the history backfill below for now.")

    # setup history
    hist = [r for r in history_rows or [] if r["kind"] == "setup_history" and r["key"] not in ("all", "benchmark")]
    if hist:
        h = max(r["horizon_days"] for r in hist)
        for r in sorted((r for r in hist if r["horizon_days"] == h), key=lambda r: -(r["mean_excess_pct"] or 0)):
            ev = (f"{r['key']}: {_pct(r['mean_ret_pct'])} average over {h} days, {_pct(r['mean_excess_pct'])} vs the "
                  f"universe on the same days, beat it in {r['years_positive']} years (n={r['n']}, "
                  f"{r['tickers']} stocks, overlapping windows)")
            if r["n"] < MIN_N:
                out.append(f"{ev}: too few to judge, keep collecting.")
            elif (r["mean_excess_pct"] or 0) >= EDGE_PCT:
                out.append(f"{ev}: a historical edge; worth ranking higher in the scan.")
            elif (r["mean_excess_pct"] or 0) <= -EDGE_PCT:
                out.append(f"{ev}: it lagged; treat these hits as information, not buy ideas.")
            else:
                out.append(f"{ev}: no clear edge either way.")

    # the odds desk
    if odds_eval:
        g = odds_eval["groups"]
        b, a, few = g["blocked"], g["allowed"], g["too_few"]
        p = odds_eval.get("params", {})
        head = (f"Odds desk backfill ({odds_eval['start']} to {odds_eval['end']}, {odds_eval['tickers']} stocks, "
                f"{len(odds_eval['strategies'])} strategies)")
        if b["n"] < MIN_N or a["n"] < MIN_N:
            out.append(f"{head}: it would have blocked {b['n']} and allowed {a['n']} trades with enough history "
                       f"(need {MIN_N} each): too few to judge, keep collecting.")
        else:
            diff = a["mean_ret_pct"] - b["mean_ret_pct"]
            ev = (f"{head}: the {b['n']} trades it would have BLOCKED averaged {_pct(b['mean_ret_pct'])} "
                  f"(won {b['win_rate']:.0%}) vs {_pct(a['mean_ret_pct'])} (won {a['win_rate']:.0%}) for the "
                  f"{a['n']} it allowed (n={b['n']}/{a['n']})")
            if diff >= 0.25:
                out.append(f"{ev}: it is screening out weaker trades, keep it.")
            elif diff <= -0.25:
                out.append(f"{ev}: blocked trades did better than allowed ones; consider loosening it "
                           f"(e.g. a lower odds_min_win_rate than {p.get('odds_min_win_rate')}, or a higher "
                           f"odds_min_signals than {p.get('odds_min_signals')}). Suggestion only: check on new data first.")
            else:
                out.append(f"{ev}: little difference; it costs trades without clearly adding value. "
                           f"Keep it as a safety net or test it on new data before changing anything.")
        if few["n"] >= MIN_N:
            out.append(f"Trades with fewer than {p.get('odds_min_signals')} past signals (never blocked) averaged "
                       f"{_pct(few['mean_ret_pct'])}, won {few['win_rate']:.0%} (n={few['n']}).")
        for name, sg in (odds_eval.get("by_strategy") or {}).items():
            bb, aa = sg["blocked"], sg["allowed"]
            if bb["n"] >= MIN_N and aa["n"] >= MIN_N:
                out.append(f"  {name}: blocked {_pct(bb['mean_ret_pct'])} (n={bb['n']}) vs allowed "
                           f"{_pct(aa['mean_ret_pct'])} (n={aa['n']}).")
    return out


# ---------------------------------------------------------------- table rows

def outcome_rows(outcomes: list[dict[str, Any]], now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Outcome rows -> `agent_outcomes` table rows (desk statuses go in the `desks` JSON column)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = []
    for r in outcomes or []:
        desks = {k: v for k, v in r.items() if k.endswith("_status") or k == "brain_decision"}
        if r.get("kind") == "scan":
            desks = {"setup": r.get("setup"), "score": r.get("score")}
        rows.append({"id": r["id"], "brief_id": r["brief_id"], "kind": r.get("kind", "brief"),
                     "as_of_date": r["as_of_date"], "ticker": r.get("ticker"), "strategy": r.get("strategy"),
                     "verdict": r.get("verdict"), "horizon_days": r["horizon_days"], "ret_pct": r.get("ret_pct"),
                     "excess_pct": r.get("excess_pct"), "mae_pct": r.get("mae_pct"), "status": r.get("status"),
                     "desks": desks, "updated_at": now})
    return rows


def _odds_scorecard(odds_eval: dict[str, Any]) -> list[dict[str, Any]]:
    out = []

    def add(key: str, group: str, s: dict[str, Any]) -> None:
        note = "realised trade return (held to the strategy's exit, net of costs); horizon 0 = per trade"
        if s["n"] < MIN_N:
            note = f"{TOO_FEW}; {note}"
        out.append({"kind": "odds_history", "key": key, "status": group, "horizon_days": 0, "n": s["n"],
                    "mean_ret_pct": s["mean_ret_pct"], "median_ret_pct": s["median_ret_pct"],
                    "hit_rate": s["win_rate"], "mean_excess_pct": None, "note": note})

    for g, s in odds_eval["groups"].items():
        add("all", g, s)
    for name, groups in odds_eval.get("by_strategy", {}).items():
        for g, s in groups.items():
            add(name, g, s)
    return out


def scorecard_rows(desk_rows: list[dict[str, Any]] | None = None, setup_rows: list[dict[str, Any]] | None = None,
                   history_rows: list[dict[str, Any]] | None = None, odds_eval: dict[str, Any] | None = None,
                   as_of: dt.date | None = None) -> list[dict[str, Any]]:
    """All scorecards -> `agent_scorecard` table rows with exactly SCORECARD_KEYS."""
    as_of = as_of or dt.date.today()
    src = list(desk_rows or []) + list(setup_rows or []) + list(history_rows or [])
    if odds_eval:
        src += _odds_scorecard(odds_eval)
    rows = []
    for r in src:
        h = int(r["horizon_days"])
        rows.append({"id": f"{r['kind']}|{r['key']}|{r['status']}|{h}", "as_of_date": as_of, "kind": r["kind"],
                     "key": str(r["key"]), "status": str(r["status"]), "horizon_days": h, "n": int(r["n"]),
                     "mean_ret_pct": r.get("mean_ret_pct"), "median_ret_pct": r.get("median_ret_pct"),
                     "hit_rate": r.get("hit_rate"), "mean_excess_pct": r.get("mean_excess_pct"),
                     "note": str(r.get("note") or "")[:500]})
    return rows


# ---------------------------------------------------------------- reading the tables

def read_remote_table(client, table: str, page: int = 1000) -> list[dict[str, Any]]:
    """Every row of a Supabase table (supabase-py client), paged."""
    rows: list[dict[str, Any]] = []
    i = 0
    while True:
        data = client.table(table).select("*").range(i, i + page - 1).execute().data or []
        rows.extend(data)
        if len(data) < page:
            return rows
        i += page


def read_rows(store, table: str) -> list[dict[str, Any]]:
    """Rows from Supabase (when configured and reachable) merged with local SQLite, by id."""
    merged: dict[str, dict[str, Any]] = {}
    try:
        for r in store.sqlite.rows(table):
            merged[str(r.get("id"))] = r
    except Exception as exc:
        log.info("Local %s unreadable: %s", table, exc)
    remote = getattr(store, "remote", None)
    client = getattr(remote, "client", None)
    if client is not None:
        try:
            for r in read_remote_table(client, table):
                merged[str(r.get("id"))] = r
        except Exception as exc:
            log.warning("Could not read %s from Supabase: %s", table, exc)
    return list(merged.values())


__all__ = ["LearningOptions", "forward_returns", "brief_outcomes", "scan_outcomes", "desk_scorecard",
           "setup_scorecard", "setup_flags", "historical_setup_scorecard", "historical_odds_evaluation",
           "suggestions", "outcome_rows", "scorecard_rows", "read_rows", "read_remote_table",
           "OUTCOME_KEYS", "SCORECARD_KEYS", "MIN_N"]
