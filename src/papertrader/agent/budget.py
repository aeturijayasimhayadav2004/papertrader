"""Position size from your max loss, then a bracket ticket. PAPER TRADING ONLY.

Whole shares only (NSE cash market). The quantity is the smallest of

    max loss       max_loss / risk per share (entry minus stop)
    position cap   account x max_position_pct / entry
    cash           cash / (entry plus buy charges)

and is then cut further, one share at a time, until the loss at the stop
INCLUDING both sides' charges is within max_loss (charges matter on small
trades). The max loss is `max_loss` rupees, or `risk_pct` percent of the
account, or the smaller of the two when both are given (default 1% of the account).

Zero shares is a plain refusal with the reasons: the stop is too far away for
your max loss, or there is not enough cash.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any

from papertrader.agent.exitplan import TradePlan
from papertrader.agent.market import now_ist
from papertrader.agent.tickets import Ticket, make_ticket_id, refuse_live
from papertrader.costs import BUY, SELL, CostModel, india_delivery, india_intraday

DEFAULT_RISK_PCT = 1.0


def default_costs(plan: TradePlan) -> CostModel:
    return india_intraday() if plan.style == "intraday" else india_delivery()


def _rs(x: float) -> str:
    return f"Rs {x:,.2f}"


def size_position(
    plan: TradePlan,
    *,
    account: float,
    max_loss: float | None = None,
    risk_pct: float | None = None,
    max_position_pct: float = 20,
    cash: float | None = None,
    costs: CostModel | None = None,
) -> dict[str, Any]:
    """Shares to buy so a stop-out loses at most the max loss (see module doc)."""
    cm = costs or default_costs(plan)
    cash = account if cash is None else cash
    limits = [x for x in (max_loss, account * risk_pct / 100 if risk_pct else None) if x is not None]
    budget_loss = min(limits) if limits else account * DEFAULT_RISK_PCT / 100
    e, s, t = plan.entry, plan.stop, plan.target
    risk = e - s
    reasons: list[str] = []

    caps = {
        "max_loss": math.floor(budget_loss / risk) if risk > 0 else 0,
        "position": math.floor(account * max_position_pct / 100 / e) if e > 0 else 0,
        "cash": math.floor(cm.max_qty(max(cash, 0.0), e)),
    }
    qty = max(0, min(caps.values()))
    binding = min(caps, key=caps.get)

    def loss_at_stop(q: int) -> float:
        return q * risk + cm.fee(BUY, q, e) + cm.fee(SELL, q, s)

    while qty > 0 and loss_at_stop(qty) > budget_loss:
        qty -= 1
        binding = "max_loss"

    if qty == 0:
        if caps["max_loss"] == 0 or binding == "max_loss":
            reasons.append(f"The stop is {_rs(risk)} a share away; with charges even 1 share would lose "
                           f"{_rs(loss_at_stop(1))}, more than your max loss of {_rs(budget_loss)}.")
        if caps["cash"] == 0:
            reasons.append(f"Not enough cash: {_rs(cash)} does not buy 1 share at {_rs(e)} plus charges.")
        if caps["position"] == 0:
            reasons.append(f"1 share at {_rs(e)} is over the {max_position_pct:g}% position cap "
                           f"({_rs(account * max_position_pct / 100)}).")
        if not reasons:
            reasons.append("No whole share fits the limits.")

    notional = qty * e
    buy_fee = cm.fee(BUY, qty, e)
    loss = loss_at_stop(qty) if qty else 0.0
    gain = qty * (t - e) - buy_fee - cm.fee(SELL, qty, t) if qty else 0.0
    return {
        "ok": qty > 0, "qty": int(qty), "ticker": plan.ticker, "entry": e, "stop": s, "target": t,
        "risk_per_share": round(risk, 2), "max_loss": round(budget_loss, 2), "notional": round(notional, 2),
        "position_pct": round(notional / account * 100, 2) if account else None,
        "buy_charges": round(buy_fee, 2),
        "loss_at_stop": round(loss, 2),           # incl. both sides' charges
        "gain_at_target": round(gain, 2),         # after both sides' charges
        "caps": caps, "binding": binding if qty else None, "costs": cm.label, "reasons": reasons,
    }


def describe_sizing(sizing: dict[str, Any]) -> str:
    if not sizing["ok"]:
        return f"No trade in {sizing['ticker']}: " + " ".join(sizing["reasons"])
    return (f"Buy {sizing['qty']} {sizing['ticker']} at {_rs(sizing['entry'])} = {_rs(sizing['notional'])} "
            f"({sizing['position_pct']:.1f}% of the account). At the stop you lose about "
            f"{_rs(sizing['loss_at_stop'])} incl. charges (limit {_rs(sizing['max_loss'])}); at the target you make "
            f"about {_rs(sizing['gain_at_target'])} after charges. Size set by the {sizing['binding']} limit.")


def bracket_ticket(plan: TradePlan, sizing: dict[str, Any], mode: str = "paper", order_type: str = "LIMIT",
                   now: dt.datetime | None = None) -> Ticket:
    """A draft BUY ticket with the plan's stop and target. Raises ValueError for zero shares."""
    refuse_live(mode)
    if not sizing.get("qty"):
        raise ValueError("no ticket for zero shares: " + " ".join(sizing.get("reasons") or []))
    created = now_ist(now)
    intraday = plan.style == "intraday"
    return Ticket(
        ticket_id=make_ticket_id(plan.ticker, created), created_at=created.isoformat(timespec="seconds"),
        ticker=plan.ticker, side="BUY", qty=int(sizing["qty"]), order_type=order_type,
        limit_price=plan.entry if order_type == "LIMIT" else None, stop_loss=plan.stop, target=plan.target,
        product="MIS" if intraday else "CNC", mode=mode, max_loss=sizing.get("loss_at_stop"),
        flat_by=plan.flat_by.strftime("%H:%M") if plan.flat_by and intraday else None,
        notes=[f"Sized by the {sizing.get('binding')} limit; {sizing.get('costs')}."],
    )
