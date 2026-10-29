"""Time machine: replay a planned trade before risking a rupee. PAPER TRADING ONLY.

Intraday (5-minute bars, IST):
  simulate_session  bootstraps the rest of today's session from the stock's own
                    recent sessions. For each remaining 5-minute slot it draws one
                    past session's bar from the same time of day (give or take
                    `window` slots), as ratios to the previous bar's close
                    (open, high, low, close), and scales it to the current price.
                    Each path walks to the plan's flat-by time. In every bar the
                    stop is checked before the target, so a bar that touches both
                    counts as the stop (the pessimistic call). A gap through the
                    stop fills at the bar's open, which can be worse than -1R.
  replay_orb_history re-runs a simple opening-range breakout on every past session:
                    entry = the close of the first completed bar after the range
                    that closes above the range high (no later than
                    `latest_entry`), same stop / target / stop-first rules.

Swing (daily bars):
  signal_odds       replays THIS strategy's past BUY signals on THIS stock with the
                    backtester (backtest.simulate_sleeve: next-open fills, same exit
                    rules and costs, no lookahead) and summarises the trades.
  odds_check        turns that into an "odds" desk Finding (BLOCK on a losing record).

A simulation built from the stock's own past is not a prediction: results,
news and circuit limits can do what the last 60 days never did.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any

import numpy as np
import pandas as pd

from papertrader.agent.exitplan import PlanError, TradePlan, plan_trade
from papertrader.agent.market import CLOSE, FLAT_BY, IST, OPEN
from papertrader.agent.types import BLOCK, INFO, Finding, unknown

log = logging.getLogger(__name__)

BAR_MIN = 5
OPEN_MIN = OPEN.hour * 60 + OPEN.minute                       # 09:15 -> 555
SESSION_SLOTS = (CLOSE.hour * 60 + CLOSE.minute - OPEN_MIN) // BAR_MIN   # 75 five-minute bars
MIN_SESSIONS = 20
COIN_FLIP, STRONG = 0.05, 0.15                                # verdict thresholds (share points)
NOT_A_PREDICTION = "This is a simulation, not a prediction."


# ---------------------------------------------------------------- bar grid

def _ist_index(df: pd.DataFrame) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(df.index)
    return idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)


def _minutes(t: dt.time) -> int:
    return t.hour * 60 + t.minute


def last_slot_before(t: dt.time | None) -> int:
    """Index of the last 5-minute bar that has finished by clock time `t` (None = the close)."""
    if t is None:
        return SESSION_SLOTS - 1
    return min(SESSION_SLOTS - 1, (_minutes(t) - OPEN_MIN) // BAR_MIN - 1)


def slot_time(j: int) -> dt.time:
    m = OPEN_MIN + BAR_MIN * j
    return dt.time(m // 60, m % 60)


def session_matrix(bars: pd.DataFrame) -> tuple[list[dt.date], dict[str, np.ndarray]]:
    """5-minute bars -> (dates, {"Open"/"High"/"Low"/"Close": array sessions x 75 slots}), NaN where missing."""
    if bars is None or bars.empty:
        return [], {k: np.empty((0, SESSION_SLOTS)) for k in ("Open", "High", "Low", "Close")}
    idx = _ist_index(bars)
    mins = idx.hour * 60 + idx.minute - OPEN_MIN
    keep = (mins >= 0) & (mins % BAR_MIN == 0) & (mins < SESSION_SLOTS * BAR_MIN)
    slots = (mins[keep] // BAR_MIN).to_numpy()
    days = idx[keep].date
    dates = sorted(set(days))
    row = {d: i for i, d in enumerate(dates)}
    rows = np.array([row[d] for d in days], dtype=int)
    out = {}
    for col in ("Open", "High", "Low", "Close"):
        m = np.full((len(dates), SESSION_SLOTS), np.nan)
        m[rows, slots] = bars[col].to_numpy(float)[keep]
        out[col] = m
    return dates, out


def _ratio_pool(M: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Each bar as ratios to the previous bar's close; slot 0 uses the previous session's last close."""
    C = M["Close"]
    prev = np.full_like(C, np.nan)
    prev[:, 1:] = C[:, :-1]
    if len(C) > 1:
        last_close = pd.DataFrame(C).ffill(axis=1).iloc[:, -1].to_numpy()
        prev[1:, 0] = last_close[:-1]
    return {k: M[k] / prev for k in ("Open", "High", "Low", "Close")}


# ---------------------------------------------------------------- intraday: the rest of today

def _lean(p_target: float, p_stop: float, r: float) -> float | None:
    if p_target + p_stop <= 0:
        return None
    return p_target / (p_target + p_stop) - 1.0 / (1.0 + r)


def _verdict(p_target: float, p_stop: float, r: float, until: str) -> str:
    d = _lean(p_target, p_stop, r)
    if d is None:
        return f"No path reached the target or the stop by {until} IST: the price at {until} decides every one."
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if abs(r - 1) < 0.01 else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    return (f"{head}: target first {p_target:.1%} vs stop first {p_stop:.1%} "
            f"(at {r:g}R the target needs {1 / (1 + r):.0%} of the paths that hit a side to break even).")


def simulate_session(history: pd.DataFrame, today: pd.DataFrame, plan: TradePlan, n: int = 10000,
                     seed: int = 19, window: int = 2, min_sessions: int = MIN_SESSIONS) -> dict[str, Any]:
    """Bootstrap the rest of today `n` times from past sessions' same-time bars (see module doc).

    Returns {"ok": False, "reason": ...} when there is too little history; otherwise
    the shares of paths that hit the target first / the stop first / were still open at
    the flat-by time, and the end P&L in R (median and mean).
    """
    today = today if today is not None else pd.DataFrame()
    t_dates, _ = session_matrix(today)
    today_date = t_dates[-1] if t_dates else None
    hist = history
    if today_date is not None and history is not None and not history.empty:
        hist = history[_ist_index(history).date < today_date]   # never learn from today itself
    dates, M = session_matrix(hist)
    pool = _ratio_pool(M)

    if today is None or today.empty:
        k, start = -1, plan.entry
    else:
        tidx = _ist_index(today)
        k = int((tidx[-1].hour * 60 + tidx[-1].minute - OPEN_MIN) // BAR_MIN)
        start = float(today["Close"].iloc[-1])
    last = last_slot_before(plan.flat_by)
    until = plan.flat_by.strftime("%H:%M") if plan.flat_by else CLOSE.strftime("%H:%M")
    steps = np.arange(k + 1, last + 1)
    risk = plan.entry - plan.stop
    r_target = (plan.target - plan.entry) / risk
    base = {"ok": True, "ticker": plan.ticker, "n": n, "seed": seed, "window": window, "flat_by": until,
            "start_price": round(start, 2), "entry": plan.entry, "stop": plan.stop, "target": plan.target,
            "r_at_target": round(r_target, 3), "bars_left": int(len(steps))}

    # Already decided before the first simulated bar.
    if start <= plan.stop or start >= plan.target or len(steps) == 0:
        outcome = "stop" if start <= plan.stop else "target" if start >= plan.target else "open"
        r_end = -1.0 if outcome == "stop" else r_target if outcome == "target" else (start - plan.entry) / risk
        base.update(sessions_used=len(dates), p_target=float(outcome == "target"),
                    p_stop=float(outcome == "stop"), p_open=float(outcome == "open"),
                    p_green=float(r_end > 0), median_end_r=round(r_end, 3), mean_r=round(r_end, 3),
                    median_end_price=round(start, 2), p5_end_price=round(start, 2), p95_end_price=round(start, 2),
                    note="Nothing left to simulate: the price is already at a side or the flat-by time has passed.")
        base["verdict"] = _verdict(base["p_target"], base["p_stop"], r_target, until)
        return base

    # Which past bars exist: Yahoo often leaves out the last bar or two of a session, so each
    # slot draws only from the sessions that have it, and a slot with too few is not used.
    Ro, Rh, Rl, Rc = (pool[c] for c in ("Open", "High", "Low", "Close"))
    valid = np.isfinite(Ro) & np.isfinite(Rh) & np.isfinite(Rl) & np.isfinite(Rc)
    count = valid.sum(axis=0)
    usable = count >= min_sessions
    usable_ids = np.flatnonzero(usable[1:]) + 1          # slot 0 (the opening gap) only stands for itself
    if (steps[0] == 0 and not usable[0]) or (len(usable_ids) == 0 and steps[-1] > 0):
        s_have = int(count[steps].min()) if len(steps) else 0
        return {**base, "ok": False, "sessions_used": s_have,
                "reason": f"only {s_have} past sessions with these bars in the data (need {min_sessions})"}

    def nearest_usable(j: int) -> int:
        if j == 0 or usable[j]:
            return j
        return int(usable_ids[np.argmin(np.abs(usable_ids - j) + (usable_ids > j) * 0.5)])  # ties: earlier slot

    mapped = np.array([nearest_usable(int(j)) for j in steps])
    used = int(count[mapped].min())
    if used < min_sessions:
        return {**base, "ok": False, "sessions_used": used,
                "reason": f"only {used} complete past sessions in the data (need {min_sessions})"}
    order = np.zeros((SESSION_SLOTS, valid.shape[0]), dtype=int)   # order[j, :count[j]] = sessions with slot j
    for j in range(SESSION_SLOTS):
        ids = np.flatnonzero(valid[:, j])
        order[j, :len(ids)] = ids

    rng = np.random.default_rng(seed)
    T = len(steps)
    base_sl = np.broadcast_to(mapped, (n, T))
    if window:
        jit = rng.integers(-window, window + 1, size=(n, T))
        sl = np.clip(base_sl + jit, 1, SESSION_SLOTS - 1)
        sl = np.where((base_sl == 0) | ~usable[sl], base_sl, sl)
    else:
        sl = base_sl
    pick = (rng.random((n, T)) * count[sl]).astype(int)
    sess = order[sl, pick]
    rc = Rc[sess, sl]
    path_close = start * np.cumprod(rc, axis=1)
    prev = np.concatenate([np.full((n, 1), start), path_close[:, :-1]], axis=1)
    op, hi, lo = prev * Ro[sess, sl], prev * Rh[sess, sl], prev * Rl[sess, sl]

    stop_hit, tgt_hit = lo <= plan.stop, hi >= plan.target
    big = T + 1
    first_stop = np.where(stop_hit.any(axis=1), stop_hit.argmax(axis=1), big)
    first_tgt = np.where(tgt_hit.any(axis=1), tgt_hit.argmax(axis=1), big)
    is_stop = (first_stop <= first_tgt) & (first_stop < big)      # ties within a bar -> the stop
    is_tgt = (first_tgt < first_stop)
    is_open = ~(is_stop | is_tgt)

    rows = np.arange(n)
    stop_fill = np.minimum(op[rows, np.minimum(first_stop, T - 1)], plan.stop)   # a gap fills at the open
    end = path_close[:, -1]
    r_end = np.where(is_stop, (stop_fill - plan.entry) / risk,
                     np.where(is_tgt, r_target, (end - plan.entry) / risk))
    base.update(
        sessions_used=used,
        p_target=round(float(is_tgt.mean()), 4), p_stop=round(float(is_stop.mean()), 4),
        p_open=round(float(is_open.mean()), 4), p_green=round(float((r_end > 0).mean()), 4),
        median_end_r=round(float(np.median(r_end)), 3), mean_r=round(float(r_end.mean()), 3),
        median_end_price=round(float(np.median(end)), 2),
        p5_end_price=round(float(np.quantile(end, 0.05)), 2), p95_end_price=round(float(np.quantile(end, 0.95)), 2),
    )
    base["verdict"] = _verdict(base["p_target"], base["p_stop"], r_target, until)
    return base


def describe_session(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"Time machine could not run: {result.get('reason')}."
    return (f"{result['ticker']}: {result['n']:,} simulated rests of today from {result['sessions_used']} past "
            f"sessions. Target first {result['p_target']:.1%}, stop first {result['p_stop']:.1%}, still open at "
            f"{result['flat_by']} IST {result['p_open']:.1%}. Median end {result['median_end_r']:+.2f}R, "
            f"average {result['mean_r']:+.2f}R. {result['verdict']} {NOT_A_PREDICTION}")


# ---------------------------------------------------------------- intraday: the ORB rule on past sessions

def replay_orb_history(history: pd.DataFrame, or_minutes: int = 15, stop_method: str = "or_mid",
                       target_r: float = 1.0, flat_by: dt.time | None = FLAT_BY,
                       latest_entry: dt.time = dt.time(14, 30), stop_pct: float | None = None) -> dict[str, Any]:
    """Opening-range breakout on every past session: fired / target / stop / neither, plus each day."""
    dates, M = session_matrix(history)
    O, H, L, C = (M[c] for c in ("Open", "High", "Low", "Close"))
    nb = max(1, or_minutes // BAR_MIN)
    last = last_slot_before(flat_by)
    latest = _minutes(latest_entry)
    days: list[dict[str, Any]] = []
    for i, d in enumerate(dates):
        if not np.all(np.isfinite(H[i, :nb])) or not np.isfinite(C[i, :last + 1]).any():
            continue
        rh, rl = float(np.max(H[i, :nb])), float(np.min(L[i, :nb]))
        trig = next((j for j in range(nb, last + 1)
                     if OPEN_MIN + BAR_MIN * (j + 1) <= latest and np.isfinite(C[i, j]) and C[i, j] > rh), None)
        day: dict[str, Any] = {"date": d.isoformat(), "range_high": round(rh, 2), "range_low": round(rl, 2),
                               "fired": trig is not None}
        if trig is None:
            days.append({**day, "outcome": "no_trigger"})
            continue
        try:
            p = plan_trade("ORB", float(C[i, trig]), stop_method=stop_method, range_high=rh, range_low=rl,
                           stop_pct=stop_pct, target_r=target_r, flat_by=flat_by)
        except PlanError as exc:
            days.append({**day, "fired": False, "outcome": "skipped", "why": str(exc)})
            continue
        risk = p.entry - p.stop
        outcome, r = "neither", None
        for j in range(trig + 1, last + 1):
            if not np.isfinite(C[i, j]):
                continue
            if L[i, j] <= p.stop:
                outcome, r = "stop", (min(O[i, j], p.stop) - p.entry) / risk
                break
            if H[i, j] >= p.target:
                outcome, r = "target", (max(O[i, j], p.target) - p.entry) / risk
                break
        if r is None:
            closes = C[i, :last + 1]
            r = (float(closes[np.isfinite(closes)][-1]) - p.entry) / risk
        days.append({**day, "entry_time": slot_time(trig + 1).strftime("%H:%M"), "entry": p.entry, "stop": p.stop,
                     "target": p.target, "outcome": outcome, "r": round(float(r), 3)})
    fired = [x for x in days if x["fired"]]
    rs = [x["r"] for x in fired]
    return {
        "sessions": len(days), "fired": len(fired),
        "target": sum(x["outcome"] == "target" for x in fired),
        "stop": sum(x["outcome"] == "stop" for x in fired),
        "neither": sum(x["outcome"] == "neither" for x in fired),
        "win_rate": round(float(np.mean([r > 0 for r in rs])), 3) if rs else None,
        "avg_r": round(float(np.mean(rs)), 3) if rs else None,
        "rule": {"or_minutes": or_minutes, "stop_method": stop_method, "target_r": target_r,
                 "flat_by": flat_by.strftime("%H:%M") if flat_by else None,
                 "latest_entry": latest_entry.strftime("%H:%M")},
        "days": days,
    }


# ---------------------------------------------------------------- swing: this strategy's past signals here

def signal_odds(df: pd.DataFrame, strategy, exits, costs, lookback_days: int = 1500,
                min_signals: int = 8, ticker: str = "") -> dict[str, Any]:
    """Replay `strategy`'s past BUY signals on this stock over the last `lookback_days`
    calendar days with the backtester (same exits and costs as the paper broker).

    Trades still open at the end are left out of the statistics (they have no result yet).
    """
    from papertrader.backtest import simulate_sleeve   # local: keeps the agent importable on its own

    if df is None or df.empty:
        raise ValueError("no daily price data")
    end = df.index[-1]
    start = max(df.index[0], end - pd.Timedelta(days=lookback_days))
    signals = strategy.signals(df)
    res = simulate_sleeve(df, signals, start.date(), end.date(), 1_000_000.0, costs, ticker=ticker, exits=exits)
    closed = [t for t in res.trades if t.exit_reason != "end_of_period"]
    rets = np.array([t.return_pct for t in closed], dtype=float) * 100
    wins, losses = rets[rets > 0], rets[rets <= 0]
    n = len(rets)
    pf = float(wins.sum() / -losses.sum()) if losses.sum() < 0 else None
    return {
        "ticker": ticker, "strategy": getattr(strategy, "name", str(strategy)),
        "from": start.date().isoformat(), "to": end.date().isoformat(),
        "n_trades": n, "enough": n >= min_signals, "min_signals": min_signals,
        "win_rate": round(float(len(wins) / n), 3) if n else None,
        "avg_return_pct": round(float(rets.mean()), 3) if n else None,
        "expectancy_pct": round(float(rets.mean()), 3) if n else None,   # average net result per trade
        "avg_win_pct": round(float(wins.mean()), 3) if len(wins) else None,
        "avg_loss_pct": round(float(losses.mean()), 3) if len(losses) else None,
        "profit_factor": round(pf, 3) if pf is not None else None,
        "median_days_held": (float(np.median([(t.exit_date - t.entry_date).days for t in closed])) if n else None),
        "exit_reasons": {r: sum(t.exit_reason == r for t in closed) for r in sorted({t.exit_reason for t in closed})},
        "open_at_end": len(res.trades) - n,
        "trades": [{"entry_date": t.entry_date.date().isoformat(), "exit_date": t.exit_date.date().isoformat(),
                    "return_pct": round(t.return_pct * 100, 2), "exit_reason": t.exit_reason} for t in closed[-20:]],
    }


def describe_odds(result: dict[str, Any]) -> str:
    """The past odds in plain words."""
    n = result.get("n_trades", 0)
    who = f"{result.get('strategy', 'this strategy')} on {result.get('ticker') or 'this stock'}"
    if not n:
        return (f"{who}: no completed past trades between {result.get('from')} and {result.get('to')}. "
                f"{NOT_A_PREDICTION}")
    pf = result.get("profit_factor")
    pf_txt = f"profit factor {pf:.2f}" if pf is not None else "no losing trades"
    text = (f"{who}, {result['from']} to {result['to']}: {n} past trades, won {result['win_rate']:.0%}, "
            f"average {result['avg_return_pct']:+.2f}% per trade after costs, {pf_txt}.")
    if not result.get("enough"):
        text += f" That is too few past signals (under {result.get('min_signals')}) to mean much."
    return f"{text} {NOT_A_PREDICTION} Past signals on one stock are a small sample."


def odds_check(df: pd.DataFrame, strategy, exits, costs, *, lookback_days: int = 1500, min_signals: int = 8,
               min_win_rate: float = 0.40, ticker: str = "") -> Finding:
    """The "odds" desk: BLOCK a buy whose past signals here lost money or rarely won. Never raises."""
    try:
        res = signal_odds(df, strategy, exits, costs, lookback_days, min_signals, ticker)
    except Exception as exc:  # bad data, strategy error, ...
        log.warning("odds check failed for %s: %s", ticker, exc)
        return unknown("odds", str(exc))
    data = {k: v for k, v in res.items() if k != "trades"}
    n = res["n_trades"]
    if n < min_signals:
        return Finding("odds", INFO, f"Too few past signals: {describe_odds(res)}", data)
    bad = []
    if res["expectancy_pct"] <= 0:
        bad.append(f"average {res['expectancy_pct']:+.2f}% per trade after costs")
    if res["win_rate"] < min_win_rate:
        bad.append(f"won only {res['win_rate']:.0%} (need {min_win_rate:.0%})")
    if bad:
        return Finding("odds", BLOCK, f"Past signals lost here ({'; '.join(bad)}). {describe_odds(res)}", data)
    return Finding("odds", INFO, describe_odds(res), data)
