"""Chart scanner: which stocks match a setup rule today. A hit is not a buy.

Daily scan (run before the open or after the close, on COMPLETED daily bars;
papertrader.data.load_prices already drops today's bar until the close):

  breakout         close above the highest high of the prior 20 sessions, on
                   volume at least 1.5x the prior 20-session average.
  bull_flag        a strong run then a tight pause (US rule): up more than 10 %
                   over the 10 sessions before the last 5, then the last 5
                   closes inside a 5 % range, and still above the 20-day average.
  reclaim_ma20     the close before was under its 20-day average, the last
                   close is above it.
  oversold         RSI 14 (Wilder) under 30.
  oversold_bounce  RSI 14 was under 30 in one of the 3 sessions before today and
                   today closed up.
  gap_up/gap_down  today's open is at least 2 % above / below yesterday's close.
  near_52w_high    close within 3 % of the 52-week (252-session) high.

Each hit gets a `score` so different setups can be ranked together; scores are
rough (bigger move, bigger volume, deeper oversold = higher), not odds.

Opening-range breakout scan (during the session, on 5-minute bars): the range
is the high and low of the first `or_minutes` from 09:15 IST, and the rule is
entry.orb_trigger, shared with entry.check_orb and the time machine: a long hit is
the first COMPLETED bar after the range, ending by `latest_entry`, that closes
above the range high. A close below the range low first kills the long and is
reported as orb_short (information only; the agent trades longs). A bar counts
only once start + bar length <= `at`. An incomplete opening range is skipped.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import numpy as np
import pandas as pd

from papertrader import indicators as ind
from papertrader.agent import entry as entry_mod
from papertrader.agent import market
from papertrader.agent.market import CLOSE, IST, OPEN
from papertrader.agent.types import symbol

log = logging.getLogger(__name__)

SETUPS = ("breakout", "bull_flag", "reclaim_ma20", "oversold", "oversold_bounce",
          "gap_up", "gap_down", "near_52w_high")
MIN_BARS = 30            # need 20 sessions of history plus the flag's 15
BREAKOUT_LOOKBACK = 20
BREAKOUT_VOLUME_X = 1.5
FLAG_RUN_PCT = 10.0
FLAG_RANGE_PCT = 5.0
GAP_PCT = 2.0
NEAR_HIGH_PCT = 3.0
YEAR = 252


def _rs(x: float) -> str:
    return f"Rs {x:,.2f}"


def _clean_daily(df: pd.DataFrame | None) -> pd.DataFrame | None:
    if df is None or len(df) == 0 or any(c not in df.columns for c in ("Open", "High", "Low", "Close", "Volume")):
        return None
    d = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
    d = d.dropna(subset=["Open", "High", "Low", "Close"])
    d = d[(d[["Open", "High", "Low", "Close"]] > 0).all(axis=1)].sort_index()
    d["Volume"] = d["Volume"].fillna(0)
    return d if len(d) >= MIN_BARS else None


def _setups_for(ticker: str, df: pd.DataFrame, wanted: set[str]) -> list[dict[str, Any]]:
    c, o, h, v = df["Close"], df["Open"], df["High"], df["Volume"]
    last, prev = float(c.iloc[-1]), float(c.iloc[-2])
    day = pd.Timestamp(df.index[-1]).date().isoformat()
    turnover = last * float(v.iloc[-1])
    chg = 100.0 * (last / prev - 1.0)
    out: list[dict[str, Any]] = []

    def hit(setup: str, detail: str, score: float, **extra: Any) -> None:
        out.append({"ticker": ticker, "setup": setup, "close": round(last, 2), "date": day,
                    "detail": f"{symbol(ticker)}: {detail}", "score": round(float(score), 2),
                    "change_pct": round(chg, 2), "turnover": round(turnover), **extra})

    r = ind.rsi(c, 14)
    ma20 = ind.sma(c, 20)

    if "breakout" in wanted:
        hi20 = float(h.iloc[-BREAKOUT_LOOKBACK - 1:-1].max())
        avg_v = float(v.iloc[-BREAKOUT_LOOKBACK - 1:-1].mean())
        vx = float(v.iloc[-1]) / avg_v if avg_v > 0 else 0.0
        if last > hi20 and vx >= BREAKOUT_VOLUME_X:
            over = 100.0 * (last / hi20 - 1.0)
            hit("breakout", f"closed {_rs(last)}, {over:.2f}% above the prior 20-day high {_rs(hi20)}, "
                f"on {vx:.1f}x the 20-day average volume.", 3.0 + over + min(vx, 5.0),
                level=round(hi20, 2), volume_x=round(vx, 2))

    if "bull_flag" in wanted:
        run = 100.0 * (float(c.iloc[-6]) / float(c.iloc[-16]) - 1.0)
        rng5 = 100.0 * (float(c.iloc[-5:].max()) - float(c.iloc[-5:].min())) / last
        m = float(ma20.iloc[-1])
        if run > FLAG_RUN_PCT and rng5 < FLAG_RANGE_PCT and not np.isnan(m) and last > m:
            flag_hi = float(h.iloc[-5:].max())
            hit("bull_flag", f"up {run:.1f}% in the 10 sessions before the last 5, then the last 5 closes held "
                f"inside a {rng5:.1f}% range; close {_rs(last)} above the 20-day average {_rs(m)}. "
                f"Flag high {_rs(flag_hi)}.", 2.0 + run / max(rng5, 0.5), level=round(flag_hi, 2))

    if "reclaim_ma20" in wanted:
        m, mp = float(ma20.iloc[-1]), float(ma20.iloc[-2])
        if not (np.isnan(m) or np.isnan(mp)) and prev < mp and last > m:
            hit("reclaim_ma20", f"closed {_rs(last)}, back above its 20-day average {_rs(m)} after closing "
                f"under it the day before ({_rs(prev)}).", 1.5 + 100.0 * (last / m - 1.0))

    rl = float(r.iloc[-1]) if not np.isnan(r.iloc[-1]) else None
    if "oversold" in wanted and rl is not None and rl < 30:
        hit("oversold", f"RSI(14) is {rl:.1f}, under 30; close {_rs(last)}.", 1.0 + (30 - rl) / 5.0,
            rsi=round(rl, 1))

    if "oversold_bounce" in wanted and rl is not None:
        recent = r.iloc[-4:-1].dropna()
        if len(recent) and float(recent.min()) < 30 and last > prev:
            low_rsi = float(recent.min())
            back = " and is back above 30" if rl >= 30 else ""
            hit("oversold_bounce", f"RSI(14) fell to {low_rsi:.1f} in the last 3 sessions; today closed up "
                f"{chg:.2f}% at {_rs(last)}, RSI now {rl:.1f}{back}.",
                2.0 + (30 - low_rsi) / 5.0 + chg + (1.0 if rl >= 30 else 0.0), rsi=round(rl, 1))

    gap = 100.0 * (float(o.iloc[-1]) / prev - 1.0)
    if "gap_up" in wanted and gap >= GAP_PCT:
        held = "held the gap" if last >= float(o.iloc[-1]) else "faded from the open"
        hit("gap_up", f"opened {_rs(float(o.iloc[-1]))}, {gap:.2f}% above yesterday's close {_rs(prev)}, "
            f"and {held} to close {_rs(last)}.", 1.0 + gap, gap_pct=round(gap, 2))
    if "gap_down" in wanted and gap <= -GAP_PCT:
        hit("gap_down", f"opened {_rs(float(o.iloc[-1]))}, {abs(gap):.2f}% below yesterday's close {_rs(prev)}; "
            f"closed {_rs(last)}.", 0.5 + abs(gap) / 2.0, gap_pct=round(gap, 2))

    if "near_52w_high" in wanted:
        yr = h.iloc[-YEAR:]
        hi52 = float(yr.max())
        dist = 100.0 * (1.0 - last / hi52)
        if dist <= NEAR_HIGH_PCT:
            span = "52-week" if len(yr) >= YEAR else f"{len(yr)}-session"
            hit("near_52w_high", f"closed {_rs(last)}, {dist:.2f}% below its {span} high {_rs(hi52)}.",
                1.0 + (NEAR_HIGH_PCT - dist), level=round(hi52, 2))
    return out


def scan_daily(prices: dict[str, pd.DataFrame], setups: list[str] | None = None) -> list[dict[str, Any]]:
    """Daily-bar setups across tickers. Hits sorted best first (score, then turnover)."""
    wanted = set(setups or SETUPS) & set(SETUPS)
    hits: list[dict[str, Any]] = []
    for ticker, raw in (prices or {}).items():
        try:
            df = _clean_daily(raw)
            if df is not None:
                hits.extend(_setups_for(ticker, df, wanted))
        except Exception as exc:  # one odd chart must not stop the scan
            log.warning("Daily scan skipped %s: %s", ticker, exc)
    hits.sort(key=lambda x: (-x["score"], -x["turnover"], x["ticker"]))
    return hits


# ---------------------------------------------------------------- opening-range breakout

def _latest_day(bars_by_ticker: dict[str, pd.DataFrame]) -> dt.date | None:
    days: set[dt.date] = set()
    for b in bars_by_ticker.values():
        try:
            if b is not None and len(b):
                days.add(entry_mod.as_ist_index(b).index[-1].date())
        except (TypeError, ValueError):
            continue
    return max(days) if days else None


def scan_orb(bars_by_ticker: dict[str, pd.DataFrame], day: dt.date | None = None, at: dt.time | None = None,
             or_minutes: int = 15, latest_entry: dt.time = dt.time(14, 30)) -> list[dict[str, Any]]:
    """Opening-range breakout hits per ticker as of `at` (IST) on `day`, by entry.orb_trigger.

    orb_long: orb_trigger says "triggered" (the same answer entry.check_orb gives).
    orb_short: orb_trigger says the long expired because a completed bar closed
    below the range low first. That is the same event that kills the long, flagged
    for information only; the agent never trades it. Tickers whose opening range is
    incomplete (orb_trigger "no_data") are skipped.
    """
    bars_by_ticker = bars_by_ticker or {}
    now = market.now_ist()
    if day is None:
        day = _latest_day(bars_by_ticker) or now.date()   # live: today; on a weekend: the last session
    if at is not None:
        clock = dt.datetime.combine(day, at, IST)
    elif day == now.date():
        clock = now
    else:
        clock = dt.datetime.combine(day, CLOSE, IST)
    or_end = dt.datetime.combine(day, OPEN, IST) + dt.timedelta(minutes=or_minutes)
    if clock < or_end:
        return []

    hits: list[dict[str, Any]] = []
    for ticker, raw in bars_by_ticker.items():
        try:
            day_bars = entry_mod.session_bars(raw, day)
            if day_bars.empty:
                continue
            step = entry_mod.infer_bar_minutes(day_bars)
            res = entry_mod.orb_trigger(day_bars, or_minutes, step, latest_entry, at=clock)
            if res.status == "triggered":
                long, start, end, close = True, res.trigger_start, res.trigger_time, res.trigger_price
            elif res.status == "expired" and res.reason == entry_mod.ORB_LOW_BREAK:
                long, start, end, close = False, res.break_start, res.break_time, res.break_price
            else:
                continue          # no_data (range incomplete), waiting, or expired at the cutoff
            hi, lo = float(res.range_high), float(res.range_low)
            pct = 100.0 * (close / hi - 1.0) if long else 100.0 * (1.0 - close / lo)
            side = "above the range high" if long else "below the range low"
            lvl = hi if long else lo
            hits.append({
                "ticker": ticker, "setup": "orb_long" if long else "orb_short",
                "close": round(close, 2), "time": end.strftime("%H:%M"),
                "range_high": round(hi, 2), "range_low": round(lo, 2), "pct_beyond": round(pct, 2),
                "score": round(pct, 2), "date": day.isoformat(),
                "detail": (f"{symbol(ticker)}: the {start:%H:%M} bar closed {_rs(close)}, {pct:.2f}% {side} "
                           f"{_rs(lvl)} (range {_rs(lo)}-{_rs(hi)}), at {end:%H:%M} IST"
                           + ("" if long else "; the long setup failed, short side is for information only")
                           + "."),
            })
        except Exception as exc:
            log.warning("ORB scan skipped %s: %s", ticker, exc)
    hits.sort(key=lambda x: (x["setup"] != "orb_long", -x["score"], x["ticker"]))
    return hits


def describe_hits(hits: list[dict[str, Any]], limit: int = 20) -> str:
    """Plain text, one line a hit, grouped by setup in the order they rank."""
    if not hits:
        return "No charts matched a setup rule."
    lines = [f"{len(hits)} chart(s) matched a setup rule (a match is not a buy signal):"]
    for h in hits[:limit]:
        lines.append(f"  [{h['setup']}] {h['detail']} (score {h['score']:.1f})")
    if len(hits) > limit:
        lines.append(f"  ... and {len(hits) - limit} more.")
    return "\n".join(lines)
