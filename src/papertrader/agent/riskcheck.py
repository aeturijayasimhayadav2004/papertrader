"""Pre-trade risk check: PASS or BLOCK for one order ticket, with the reasons. PAPER ONLY.

Every check runs (so you see all the problems at once) and each one is listed
with ok / detail. Any failed check blocks the ticket:

  mode          the ticket must be paper (live is refused outright)
  side          buys only; short selling is off unless allow_short (and still unsupported)
  quantity      at least 1 whole share
  stop          the stop is below the entry and the target above it
  max loss      loss at the stop (incl. charges when the ticket carries it) within max_loss_per_trade
  daily loss    today's realised loss plus this trade's loss at the stop within max_daily_loss
  trades        fewer than max_trades_per_day entries today
  reward:risk   (target - entry) / (entry - stop) at least min_reward_risk
  window        intraday (MIS) only: NSE open (holidays from market.HOLIDAYS) and the
                clock inside the entry window (09:20-14:30 IST by default). A delivery
                (CNC) ticket fills at the next open, so only "is that a trading day" applies.
  position      notional within max_position_pct of the account
  cash          notional plus buy charges within cash
  circuit band  entry within max_move_from_prev_close_pct of the previous close
                (a proxy for NSE's price bands; a stock near its band can freeze)

The entry checked is the ticket's limit price, or the plan's entry for a market order.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any

from papertrader.agent import market
from papertrader.agent.exitplan import TradePlan
from papertrader.agent.tickets import Ticket
from papertrader.costs import BUY, CostModel, india_delivery, india_intraday

EPS = 1e-6


@dataclass(frozen=True)
class RiskLimits:
    max_loss_per_trade: float = 1_000.0
    max_daily_loss: float = 3_000.0
    max_trades_per_day: int = 3
    min_reward_risk: float = 1.0
    max_position_pct: float = 20.0
    entry_start: dt.time = dt.time(9, 20)
    entry_end: dt.time = dt.time(14, 30)
    max_move_from_prev_close_pct: float = 20.0
    allow_short: bool = False


@dataclass
class RiskVerdict:
    verdict: str                                   # PASS | BLOCK
    reasons: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def describe(self) -> str:
        if self.passed:
            return "Risk check PASS: " + "; ".join(c["detail"] for c in self.checks)
        return "Risk check BLOCK: " + "; ".join(self.reasons)


def _rs(x: float) -> str:
    return f"Rs {x:,.2f}"


def check_ticket(
    ticket: Ticket,
    plan: TradePlan,
    *,
    limits: RiskLimits,
    account: float,
    cash: float,
    trades_today: int,
    realised_pnl_today: float,
    now: dt.datetime | None = None,
    prev_close: float | None = None,
    costs: CostModel | None = None,
) -> RiskVerdict:
    """Check one ticket. Sets ticket.status to risk_passed or rejected and ticket.risk_reasons."""
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    intraday = ticket.product == "MIS"
    cm = costs or (india_intraday() if intraday else india_delivery())
    entry = ticket.limit_price if ticket.order_type == "LIMIT" and ticket.limit_price else plan.entry
    stop, target, qty = ticket.stop_loss, ticket.target, ticket.qty
    risk = entry - stop

    check("mode", ticket.mode == "paper", "paper money" if ticket.mode == "paper"
          else f"mode {ticket.mode!r}: only paper trading is allowed")
    long_ok = ticket.side == "BUY" and plan.side == "long"
    check("side", long_ok or limits.allow_short, "buy (long)" if long_ok else "short selling is not allowed")
    check("quantity", qty >= 1, f"{qty} shares" if qty >= 1 else "zero shares")
    check("stop", stop < entry < target,
          f"stop {_rs(stop)} < entry {_rs(entry)} < target {_rs(target)}" if stop < entry < target
          else f"stop {_rs(stop)}, entry {_rs(entry)}, target {_rs(target)} are not in that order")

    loss = ticket.max_loss if ticket.max_loss is not None else qty * max(risk, 0.0)
    check("max_loss", loss <= limits.max_loss_per_trade + EPS,
          f"loss at the stop {_rs(loss)} vs limit {_rs(limits.max_loss_per_trade)} per trade")

    lost_today = max(0.0, -realised_pnl_today)
    check("daily_loss", lost_today + loss <= limits.max_daily_loss + EPS,
          f"lost {_rs(lost_today)} today, plus up to {_rs(loss)} on this trade, vs daily limit "
          f"{_rs(limits.max_daily_loss)}")
    check("trades", trades_today < limits.max_trades_per_day,
          f"{trades_today} trade(s) today, limit {limits.max_trades_per_day}")

    rr = (target - entry) / risk if risk > 0 else 0.0
    check("reward_risk", rr >= limits.min_reward_risk - 0.005,
          f"reward:risk {rr:.2f} vs minimum {limits.min_reward_risk:g}")

    status = market.market_status(now)
    t = status.now
    if intraday:
        in_window = limits.entry_start <= t.time() <= limits.entry_end
        ok = status.is_open and in_window
        if not status.is_open:
            detail = f"market not open: {status.describe()}"
        elif not in_window:
            detail = (f"{t:%H:%M} IST is outside the entry window "
                      f"{limits.entry_start:%H:%M}-{limits.entry_end:%H:%M} IST")
        else:
            detail = f"{t:%H:%M} IST, inside the entry window {limits.entry_start:%H:%M}-{limits.entry_end:%H:%M}"
        check("window", ok, detail)
    else:
        fill_day = market.next_trading_day(t.date(), include_today=t.time() < market.OPEN)
        check("window", True, f"delivery order, fills at the open on {fill_day:%a %d %b}")

    notional = qty * entry
    cap = account * limits.max_position_pct / 100
    check("position", notional <= cap + EPS,
          f"position {_rs(notional)} vs cap {_rs(cap)} ({limits.max_position_pct:g}% of {_rs(account)})")
    need = notional + cm.fee(BUY, qty, entry)
    check("cash", need <= cash + EPS, f"needs {_rs(need)} incl. charges, cash {_rs(cash)}")

    if prev_close:
        move = (entry / prev_close - 1) * 100
        check("circuit_band", abs(move) <= limits.max_move_from_prev_close_pct + EPS,
              f"entry is {move:+.1f}% from the previous close {_rs(prev_close)} "
              f"(band proxy {limits.max_move_from_prev_close_pct:g}%)")
    else:
        check("circuit_band", True, "no previous close given; band not checked")

    reasons = [c["detail"] for c in checks if not c["ok"]]
    verdict = RiskVerdict("BLOCK" if reasons else "PASS", reasons, checks)
    ticket.status = "risk_passed" if verdict.passed else "rejected"
    ticket.risk_reasons = reasons
    return verdict
