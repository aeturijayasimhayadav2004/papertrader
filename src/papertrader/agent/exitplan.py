"""Exit planner: the stop and the target, set BEFORE the entry. Long side only.

The stop is where the setup has failed; the target is where you take what you
came for. Both are written down first so nobody decides anything in the middle
of a fast move. A stop is a plan, not a guarantee: a gap or a fast market can
fill past it.

Stop methods:
  pct      `stop_pct` percent below the entry
  atr      `atr_multiple` x ATR below the entry
  or_mid   the opening range's midpoint (the default for an opening-range breakout)
  or_low   the opening range's low
  price    a price you give

Target: entry + `target_r` x risk, where risk = entry - stop (1R = one times the risk).

Rounding to NSE's tick size (Rs 0.05), always in the cautious direction:
  * entry rounds UP   (a buy limit you might have to pay a tick more for)
  * stop rounds DOWN  (away from the entry: the risk is never understated, and
                       a stop one tick lower survives a little more noise)
  * target rounds DOWN (towards the entry: a sell at the target is a tick
                       easier to fill, and the reward is never overstated)
So the R multiple shown can be a hair under `target_r`, never over it.

Intraday plans are flat by FLAT_BY (15:15 IST), before brokers square off MIS
positions; swing plans have no flat-by time.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from papertrader.agent.market import FLAT_BY

TICK = 0.05
MIN_RISK_PCT = 0.05      # a stop closer than 0.05% of the price is inside the spread's noise
STOP_METHODS = ("pct", "atr", "or_mid", "or_low", "price")
STYLES = ("intraday", "swing")


class PlanError(ValueError):
    """The plan does not make sense (stop at or above the entry, missing inputs, ...)."""


def _ticks(x: float) -> float:
    return round(x / TICK, 6)  # 6 dp kills float noise such as 21721.999999


def tick_down(x: float) -> float:
    return round(math.floor(_ticks(x)) * TICK, 2)


def tick_up(x: float) -> float:
    return round(math.ceil(_ticks(x)) * TICK, 2)


def _clock(t: dt.time | str | None) -> dt.time | None:
    if t is None or isinstance(t, dt.time):
        return t
    return dt.datetime.strptime(str(t), "%H:%M").time()


@dataclass
class TradePlan:
    ticker: str
    side: str                      # "long" only
    entry: float
    stop: float
    target: float
    risk_per_share: float          # entry - stop
    reward_per_share: float        # target - entry
    r_multiple: float              # reward / risk
    flat_by: dt.time | None        # IST; None for swing trades
    style: str = "intraday"        # intraday | swing
    notes: list[str] = field(default_factory=list)
    stop_rule: str = ""            # how the stop was set, e.g. "or_mid"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["flat_by"] = self.flat_by.strftime("%H:%M") if self.flat_by else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TradePlan":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        known["flat_by"] = _clock(known.get("flat_by"))
        known["notes"] = list(known.get("notes") or [])
        return cls(**known)


def _raw_stop(entry: float, method: str, *, stop_pct, atr, atr_multiple, range_high, range_low,
              stop_price) -> tuple[float, str]:
    if method == "pct":
        if not stop_pct or stop_pct <= 0:
            raise PlanError("stop_method 'pct' needs a positive stop_pct")
        return entry * (1 - stop_pct / 100.0), f"{stop_pct:g}% below the entry"
    if method == "atr":
        if not atr or atr <= 0 or math.isnan(atr):
            raise PlanError("stop_method 'atr' needs a positive ATR")
        return entry - atr_multiple * atr, f"{atr_multiple:g} x ATR (Rs {atr:,.2f}) below the entry"
    if method in ("or_mid", "or_low"):
        if range_high is None or range_low is None or not range_low < range_high:
            raise PlanError(f"stop_method {method!r} needs the opening range (range_high > range_low)")
        if method == "or_mid":
            return (range_high + range_low) / 2, "opening-range midpoint"
        return range_low, "opening-range low"
    if method == "price":
        if stop_price is None:
            raise PlanError("stop_method 'price' needs stop_price")
        return float(stop_price), "your price"
    raise PlanError(f"unknown stop_method {method!r}; use one of {', '.join(STOP_METHODS)}")


def plan_trade(
    ticker: str,
    entry: float,
    *,
    stop_method: str = "pct",
    stop_pct: float | None = None,
    atr: float | None = None,
    atr_multiple: float = 2.0,
    range_high: float | None = None,
    range_low: float | None = None,
    stop_price: float | None = None,
    target_r: float = 1.0,
    style: str = "intraday",
    flat_by: dt.time | None = FLAT_BY,
) -> TradePlan:
    """Stop and target for a long trade, rounded to Rs 0.05 ticks. Raises PlanError if it makes no sense."""
    if style not in STYLES:
        raise PlanError(f"style must be one of {STYLES}")
    if not entry or entry <= 0 or math.isnan(entry):
        raise PlanError("the entry must be a positive price")
    if not target_r or target_r <= 0:
        raise PlanError("target_r must be positive (1.0 = one times the risk)")
    e = tick_up(entry)
    raw, label = _raw_stop(e, stop_method, stop_pct=stop_pct, atr=atr, atr_multiple=atr_multiple,
                           range_high=range_high, range_low=range_low, stop_price=stop_price)
    stop = tick_down(raw)
    if stop <= 0:
        raise PlanError(f"the stop works out at Rs {stop:,.2f}; it must be above zero")
    if stop >= e:
        raise PlanError(f"the stop (Rs {stop:,.2f}, {label}) is not below the entry (Rs {e:,.2f}); "
                        "for a long trade the stop must be under the entry")
    risk = round(e - stop, 2)
    if risk < e * MIN_RISK_PCT / 100:
        raise PlanError(f"the stop is only Rs {risk:,.2f} under the entry, less than {MIN_RISK_PCT}% of the "
                        "price: too tight, normal wiggle would stop you out. Widen it.")
    target = tick_down(e + target_r * risk)
    if target <= e:
        raise PlanError(f"the target (Rs {target:,.2f}) is not above the entry (Rs {e:,.2f})")
    reward = round(target - e, 2)
    notes = [f"Stop: {label}, rounded down to the Rs {TICK:.2f} tick.",
             f"Target: {target_r:g}R, rounded down to the tick (never overstates the reward)."]
    if style == "swing":
        flat_by = None
    elif flat_by is None:
        notes.append("No flat-by time: the broker will square off MIS positions near 15:15-15:25 IST anyway.")
    return TradePlan(ticker.upper(), "long", e, stop, target, risk, reward, round(reward / risk, 2),
                     _clock(flat_by), style, notes, stop_method)


def describe_plan(plan: TradePlan) -> str:
    """The plan in plain words."""
    pct = plan.risk_per_share / plan.entry * 100
    lines = [
        f"{plan.ticker}: buy at Rs {plan.entry:,.2f}, stop Rs {plan.stop:,.2f}, target Rs {plan.target:,.2f}.",
        f"Risk Rs {plan.risk_per_share:,.2f} a share ({pct:.2f}% of the price); reward Rs "
        f"{plan.reward_per_share:,.2f} a share, so {plan.r_multiple:g}R.",
    ]
    if plan.flat_by:
        lines.append(f"Out by {plan.flat_by:%H:%M} IST whatever happens (intraday).")
    else:
        lines.append("Swing trade: held overnight until the stop, the target or the strategy's exit.")
    lines += plan.notes
    lines.append("A stop is a plan, not a guarantee: a gap can fill past it.")
    return "\n".join(lines)
