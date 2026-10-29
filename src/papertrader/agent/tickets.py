"""Order tickets and the intraday paper ledger. PAPER TRADING ONLY.

Nothing here talks to a broker. A ticket goes

    draft -> risk_passed (riskcheck.check_ticket) -> approved (your exact "yes") -> executed (paper fill)
          \\-> rejected (risk BLOCK)                                    (or expired)

`mode` must be "paper"; anything else raises LiveTradingRefused, so no code path can
turn a ticket into a real order. `approve` accepts only the single word "yes" (any
case, surrounding spaces ignored) and only for a ticket that passed the risk check.

The intraday paper ledger (data/agent/paper_ledger.json) is separate from the daily
PaperBroker's state: `execute_paper` appends a simulated fill, `close_paper` records
the exit and the P&L after charges. Both are idempotent by ticket_id, so a retry
never records a trade twice.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from papertrader.agent.market import IST, now_ist
from papertrader.agent.types import symbol
from papertrader.config import PROJECT_ROOT
from papertrader.costs import BUY, SELL, CostModel, india_delivery, india_intraday

AGENT_DIR = PROJECT_ROOT / "data" / "agent"
DEFAULT_TICKETS = AGENT_DIR / "tickets.json"
DEFAULT_LEDGER = AGENT_DIR / "paper_ledger.json"

STATUSES = ("draft", "risk_passed", "approved", "executed", "rejected", "expired")
ORDER_TYPES = ("LIMIT", "MARKET")
PRODUCTS = ("MIS", "CNC")


class LiveTradingRefused(RuntimeError):
    """Raised for anything but paper mode. This project never places real orders."""


def refuse_live(mode: str) -> None:
    if mode != "paper":
        raise LiveTradingRefused(f"mode {mode!r} refused: this agent trades PAPER MONEY only and has no "
                                 "live broker code. Use mode='paper'.")


def make_ticket_id(ticker: str, created_at: dt.datetime) -> str:
    """'INFY-20261029-101500': the same ticker at the same second gives the same id."""
    return f"{symbol(ticker)}-{now_ist(created_at):%Y%m%d-%H%M%S}"


@dataclass
class Ticket:
    ticket_id: str
    created_at: str                    # ISO time, IST
    ticker: str
    side: str                          # "BUY" only
    qty: int
    order_type: str                    # LIMIT | MARKET
    limit_price: float | None
    stop_loss: float
    target: float
    product: str = "MIS"               # MIS (intraday) | CNC (delivery)
    mode: str = "paper"                # paper only
    status: str = "draft"
    risk_reasons: list[str] = field(default_factory=list)
    approved_at: str | None = None
    notes: list[str] = field(default_factory=list)
    max_loss: float | None = None      # loss at the stop incl. round-trip charges, from budget.size_position
    flat_by: str | None = None         # "HH:MM" IST for intraday tickets

    def __post_init__(self) -> None:
        refuse_live(self.mode)
        if self.side != "BUY":
            raise ValueError("only BUY tickets: short selling is not supported")
        if self.order_type not in ORDER_TYPES:
            raise ValueError(f"order_type must be one of {ORDER_TYPES}")
        if self.product not in PRODUCTS:
            raise ValueError(f"product must be one of {PRODUCTS}")
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        if self.order_type == "LIMIT" and not self.limit_price:
            raise ValueError("a LIMIT ticket needs a limit_price")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Ticket":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


# ---------------------------------------------------------------- JSON stores

def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _write_json(path: Path, obj: Any) -> None:
    """Atomic write: a crash mid-write never leaves half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    os.replace(tmp, path)


def save_tickets(path: str | Path | None, tickets: list[Ticket]) -> None:
    _write_json(Path(path or DEFAULT_TICKETS), [t.to_dict() for t in tickets])


def load_tickets(path: str | Path | None = None) -> list[Ticket]:
    return [Ticket.from_dict(d) for d in _read_json(Path(path or DEFAULT_TICKETS), [])]


def load_ledger(path: str | Path | None = None) -> list[dict[str, Any]]:
    return _read_json(Path(path or DEFAULT_LEDGER), [])


# ---------------------------------------------------------------- words

def _rs(x: float) -> str:
    return f"Rs {x:,.2f}"


def render(ticket: Ticket) -> str:
    """The ticket in plain words, the way you would read it before saying yes."""
    price = ticket.limit_price
    how = f"at up to {_rs(price)} (limit)" if ticket.order_type == "LIMIT" else "at the market price"
    ref = price or 0.0
    loss = ticket.max_loss if ticket.max_loss is not None else ticket.qty * (ref - ticket.stop_loss)
    gain = ticket.qty * (ticket.target - ref) if ref else None
    product = "intraday (MIS)" if ticket.product == "MIS" else "delivery (CNC)"
    lines = [f"Ticket {ticket.ticket_id} [{ticket.status.upper()}] - PAPER MONEY",
             f"Buy {ticket.qty} {symbol(ticket.ticker)} {how}, {product}.",
             f"Stop-loss {_rs(ticket.stop_loss)}, target {_rs(ticket.target)}.",
             f"Max loss at the stop about {_rs(loss)} including charges"
             + (f"; about {_rs(gain)} before charges at the target." if gain is not None else ".")]
    if ticket.flat_by:
        lines.append(f"Out by {ticket.flat_by} IST whatever happens.")
    if ticket.risk_reasons:
        lines.append("Risk check: " + "; ".join(ticket.risk_reasons))
    if ticket.status == "risk_passed":
        lines.append('Reply "yes" to approve this one ticket. Anything else leaves it unapproved.')
    lines.append("PAPER MONEY: a simulated order, nothing is sent to a broker.")
    return "\n".join(lines)


# ---------------------------------------------------------------- your yes

def approve(ticket: Ticket, answer: str, now: dt.datetime | None = None) -> Ticket:
    """Approve only on the exact answer "yes" and only after the risk check passed. Mutates and returns the ticket."""
    refuse_live(ticket.mode)
    if ticket.status != "risk_passed":
        ticket.notes.append(f"Not approved: status is {ticket.status}, it must pass the risk check first.")
        return ticket
    if (answer or "").strip().lower() != "yes":
        ticket.notes.append(f"Not approved: the answer was {answer!r}, only \"yes\" approves.")
        return ticket
    ticket.status = "approved"
    ticket.approved_at = now_ist(now).isoformat(timespec="seconds")
    return ticket


# ---------------------------------------------------------------- the paper ledger

def _costs_for(product: str) -> CostModel:
    return india_intraday() if product == "MIS" else india_delivery()


def execute_paper(ticket: Ticket, fill_price: float, now: dt.datetime | None = None,
                  ledger_path: str | Path | None = None, costs: CostModel | None = None) -> dict[str, Any]:
    """Record a simulated fill for an approved ticket. A second call for the same ticket returns the first record."""
    refuse_live(ticket.mode)
    path = Path(ledger_path or DEFAULT_LEDGER)
    ledger = load_ledger(path)
    for rec in ledger:
        if rec["ticket_id"] == ticket.ticket_id:
            ticket.status = "executed"
            return rec
    if ticket.status != "approved":
        raise ValueError(f"ticket {ticket.ticket_id} is {ticket.status}; only an approved ticket can be filled")
    if not fill_price or fill_price <= 0 or math.isnan(fill_price):
        raise ValueError("fill_price must be a positive price")
    if ticket.order_type == "LIMIT" and fill_price > ticket.limit_price + 1e-9:
        raise ValueError(f"fill {_rs(fill_price)} is above the limit {_rs(ticket.limit_price)}: a buy limit "
                         "never fills higher")
    cm = costs or _costs_for(ticket.product)
    rec = {"ticket_id": ticket.ticket_id, "ticker": ticket.ticker, "product": ticket.product, "mode": "paper",
           "qty": ticket.qty, "entry_price": round(float(fill_price), 2),
           "entry_time": now_ist(now).isoformat(timespec="seconds"), "stop_loss": ticket.stop_loss,
           "target": ticket.target, "entry_charges": round(cm.fee(BUY, ticket.qty, fill_price), 2),
           "status": "open", "exit_price": None, "exit_time": None, "exit_reason": None,
           "exit_charges": None, "gross_pnl": None, "net_pnl": None, "r": None}
    ledger.append(rec)
    _write_json(path, ledger)
    ticket.status = "executed"
    return rec


def close_paper(ticket_id: str, exit_price: float, reason: str, now: dt.datetime | None = None,
                ledger_path: str | Path | None = None, costs: CostModel | None = None) -> dict[str, Any]:
    """Close an open paper trade: P&L after both sides' charges, and the result in R. Idempotent."""
    path = Path(ledger_path or DEFAULT_LEDGER)
    ledger = load_ledger(path)
    rec = next((r for r in ledger if r["ticket_id"] == ticket_id), None)
    if rec is None:
        raise KeyError(f"no paper trade for ticket {ticket_id}")
    if rec["status"] == "closed":
        return rec
    if not exit_price or exit_price <= 0:
        raise ValueError("exit_price must be a positive price")
    cm = costs or _costs_for(rec["product"])
    qty, entry = rec["qty"], rec["entry_price"]
    gross = qty * (exit_price - entry)
    exit_charges = cm.fee(SELL, qty, exit_price)
    risk = qty * (entry - rec["stop_loss"])
    rec.update(status="closed", exit_price=round(float(exit_price), 2),
               exit_time=now_ist(now).isoformat(timespec="seconds"), exit_reason=reason,
               exit_charges=round(exit_charges, 2), gross_pnl=round(gross, 2),
               net_pnl=round(gross - rec["entry_charges"] - exit_charges, 2),
               r=round(gross / risk, 3) if risk > 0 else None)
    _write_json(path, ledger)
    return rec


def ledger_today(ledger: list[dict[str, Any]], day: dt.date | None = None) -> dict[str, Any]:
    """Trades opened and net P&L realised on `day` (IST), for the risk check's daily limits."""
    day = day or dt.datetime.now(IST).date()
    opened = [r for r in ledger if r["entry_time"][:10] == day.isoformat()]
    closed = [r for r in ledger if r.get("exit_time") and r["exit_time"][:10] == day.isoformat()]
    return {"trades_today": len(opened), "realised_pnl_today": round(sum(r["net_pnl"] for r in closed), 2)}
