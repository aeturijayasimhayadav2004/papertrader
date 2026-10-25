"""Circuit breakers on top of the per-trade risk rules (modelled on Freqtrade "protections").

Each rule looks only at closed trades and equity history up to the current day,
so it is deterministic and can be recomputed from saved state:

  * cooldown        - no re-entry into a stock for N days after exiting it.
  * stoploss_guard  - N stop-losses within the lookback window pause ALL new buys.
  * max_drawdown    - a drawdown beyond the limit within the lookback window pauses ALL new buys.
  * low_profit      - a stock whose last N trades lost money in total is locked for a while.

Locks only block new buys. Exits and stop-losses always run.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterable

from papertrader.config import ProtectionConfig

HISTORY_DAYS = 400  # how much trade / equity history to keep in state


@dataclass(frozen=True)
class Lock:
    until: dt.date  # first day the lock no longer applies
    reason: str


@dataclass
class ProtectionStatus:
    global_lock: Lock | None = None
    ticker_locks: dict[str, Lock] = field(default_factory=dict)

    def blocks(self, ticker: str) -> str | None:
        """Reason a new buy of `ticker` is blocked, or None."""
        if self.global_lock:
            return self.global_lock.reason
        lock = self.ticker_locks.get(ticker)
        return lock.reason if lock else None


def _d(v: Any) -> dt.date:
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10])


def evaluate(day: dt.date, cfg: ProtectionConfig, trades: Iterable[dict[str, Any]],
             equity_history: Iterable[tuple[Any, float]]) -> ProtectionStatus:
    """Locks in force for buy decisions made at `day`'s close.

    trades: dicts with ticker, exit_date, exit_reason, return_pct (closed on or before `day`).
    equity_history: (date, equity) pairs up to and including `day`.
    """
    closed = sorted(({**t, "exit_date": _d(t["exit_date"])} for t in trades if _d(t["exit_date"]) <= day),
                    key=lambda t: t["exit_date"])
    status = ProtectionStatus()
    global_locks: list[Lock] = []

    # stoploss guard
    if cfg.stoploss_guard_lookback_days > 0:
        stops = [t["exit_date"] for t in closed if t["exit_reason"] == "stop_loss"]
        for trig in stops:
            window = [s for s in stops if trig - dt.timedelta(days=cfg.stoploss_guard_lookback_days) < s <= trig]
            until = trig + dt.timedelta(days=cfg.stoploss_guard_pause_days)
            if len(window) >= cfg.stoploss_guard_max_stops and day < until:
                global_locks.append(Lock(until, (
                    f"Protection: {len(window)} stop-losses within {cfg.stoploss_guard_lookback_days} days "
                    f"(limit {cfg.stoploss_guard_max_stops}); new buys paused until {until}.")))

    # max drawdown
    if cfg.max_drawdown_pct:
        hist = sorted((_d(d), float(e)) for d, e in equity_history if _d(d) <= day)
        for i, (trig, eq) in enumerate(hist):
            start = trig - dt.timedelta(days=cfg.max_drawdown_lookback_days)
            peak = max(e for d, e in hist[: i + 1] if d > start)
            dd = eq / peak - 1.0 if peak > 0 else 0.0
            until = trig + dt.timedelta(days=cfg.max_drawdown_pause_days)
            if dd <= -cfg.max_drawdown_pct / 100.0 and day < until:
                global_locks.append(Lock(until, (
                    f"Protection: equity fell {abs(dd):.1%} from its {cfg.max_drawdown_lookback_days}-day peak "
                    f"(limit {cfg.max_drawdown_pct:g}%); new buys paused until {until}.")))
    if global_locks:
        status.global_lock = max(global_locks, key=lambda lk: lk.until)

    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for t in closed:
        by_ticker.setdefault(t["ticker"], []).append(t)
    for ticker, ts in by_ticker.items():
        locks: list[Lock] = []
        last_exit = ts[-1]["exit_date"]
        if cfg.cooldown_days > 0:
            until = last_exit + dt.timedelta(days=cfg.cooldown_days)
            if day < until:
                locks.append(Lock(until, f"Protection: cooling down after the exit on {last_exit}; "
                                         f"no re-entry until {until}."))
        n = cfg.low_profit_lookback_trades
        if n > 0 and len(ts) >= n:
            total = sum(float(t["return_pct"]) for t in ts[-n:])
            until = last_exit + dt.timedelta(days=cfg.low_profit_lock_days)
            if total < 0 and day < until:
                locks.append(Lock(until, f"Protection: the last {n} trades in this stock lost {total:.1%} "
                                         f"in total; locked until {until}."))
        if locks:
            status.ticker_locks[ticker] = max(locks, key=lambda lk: lk.until)
    return status


def prune(day: dt.date, trades: list[dict[str, Any]], equity_history: list[list[Any]]):
    """Drop history older than HISTORY_DAYS (keeps saved state small)."""
    cutoff = day - dt.timedelta(days=HISTORY_DAYS)
    return ([t for t in trades if _d(t["exit_date"]) > cutoff],
            [e for e in equity_history if _d(e[0]) > cutoff])
