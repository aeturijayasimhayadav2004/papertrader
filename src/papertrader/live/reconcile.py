"""Reconciliation: does the broker agree with what the bot thinks it owns?

Shadow mode runs this every day. Any difference in share quantities is CRITICAL:
the bot would size, stop and exit positions that do not exist (or miss ones that do).
Cash and average-price differences are warnings / information, because fees and
rounding make small gaps normal.

Broker quantity per symbol = settled holdings (+ T1) + today's CNC net position.
MIS positions are reported separately: the bot's daily strategies only hold CNC.

Severities: "info" (fine, for the record), "warning" (look at it today),
"critical" (stop: do not trade until it is explained). A report is clean when it has
no warning or critical rows.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from papertrader.live.interface import CNC, MIS, BrokerPosition, BrokerSession, Funds, Holding, nse_symbol

INFO, WARNING, CRITICAL = "info", "warning", "critical"
SEVERITY_ORDER = {INFO: 0, WARNING: 1, CRITICAL: 2}


@dataclass(frozen=True)
class Difference:
    kind: str            # qty_mismatch | missing_at_broker | extra_at_broker | mis_open | cash | avg_price
    symbol: str          # "" for account-level rows (cash)
    bot: Any
    broker: Any
    severity: str
    message: str


@dataclass
class ReconcileReport:
    as_of: str
    differences: list[Difference] = field(default_factory=list)
    checked_symbols: int = 0

    @property
    def clean(self) -> bool:
        return not any(d.severity in (WARNING, CRITICAL) for d in self.differences)

    @property
    def worst(self) -> str:
        if not self.differences:
            return INFO
        return max((d.severity for d in self.differences), key=SEVERITY_ORDER.__getitem__)

    def summary(self) -> str:
        if self.clean:
            return f"Reconciliation {self.as_of}: clean ({self.checked_symbols} symbol(s) checked)."
        bad = [d for d in self.differences if d.severity != INFO]
        lines = [f"Reconciliation {self.as_of}: {len(bad)} difference(s), worst {self.worst.upper()}."]
        lines += [f"- [{d.severity.upper()}] {d.message}" for d in bad]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"as_of": self.as_of, "clean": self.clean, "worst": self.worst,
                "checked_symbols": self.checked_symbols, "differences": [asdict(d) for d in self.differences]}

    def save(self, path: Path | str) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")
        return p


def _bot_quantities(bot_positions: Mapping[str, Any]) -> dict[str, tuple[int, float | None]]:
    """Accept {ticker: qty} or {ticker: Position-like with .qty / .entry_price} or dicts."""
    out: dict[str, tuple[int, float | None]] = {}
    for ticker, pos in bot_positions.items():
        if isinstance(pos, (int, float)):
            qty, price = int(pos), None
        elif isinstance(pos, Mapping):
            qty, price = int(pos.get("qty", 0)), pos.get("entry_price")
        else:
            qty, price = int(getattr(pos, "qty", 0)), getattr(pos, "entry_price", None)
        sym = nse_symbol(ticker)
        prev_qty, _ = out.get(sym, (0, None))
        out[sym] = (prev_qty + qty, price)
    return {s: v for s, v in out.items() if v[0] != 0}


def broker_quantities(holdings: Iterable[Holding], positions: Iterable[BrokerPosition]
                      ) -> tuple[dict[str, tuple[int, float | None]], dict[str, int]]:
    """(CNC quantity + average price per symbol, open MIS quantity per symbol)."""
    cnc: dict[str, tuple[int, float | None]] = {}
    for h in holdings:
        qty, _ = cnc.get(h.symbol, (0, None))
        cnc[h.symbol] = (qty + h.total_qty, h.avg_price)
    mis: dict[str, int] = {}
    for p in positions:
        if p.product == CNC:
            qty, avg = cnc.get(p.symbol, (0, None))
            cnc[p.symbol] = (qty + p.qty, avg if avg is not None else p.avg_price)
        elif p.product == MIS and p.qty:
            mis[p.symbol] = mis.get(p.symbol, 0) + p.qty
    return {s: v for s, v in cnc.items() if v[0] != 0}, mis


def reconcile(bot_positions: Mapping[str, Any], holdings: Iterable[Holding], positions: Iterable[BrokerPosition] = (),
              *, bot_cash: float | None = None, funds: Funds | None = None, as_of: dt.date | str | None = None,
              ignore_symbols: Iterable[str] = (), cash_tolerance_abs: float = 100.0,
              cash_tolerance_pct: float = 1.0, avg_price_tolerance_pct: float = 2.0) -> ReconcileReport:
    """Compare the bot's positions (and optionally cash) with the broker's.

    `ignore_symbols`: shares you hold by hand that the bot must leave alone (reported as info).
    Cash is compared only when both `bot_cash` and `funds` are given.
    """
    report = ReconcileReport(as_of=str(as_of or dt.date.today()))
    ignore = {nse_symbol(s) for s in ignore_symbols}
    bot = _bot_quantities(bot_positions)
    broker, mis = broker_quantities(holdings, positions)
    symbols = sorted(set(bot) | set(broker))
    report.checked_symbols = len(symbols)
    add = report.differences.append

    for sym in symbols:
        b_qty, b_px = bot.get(sym, (0, None))
        k_qty, k_px = broker.get(sym, (0, None))
        if sym in ignore:
            add(Difference("ignored", sym, b_qty, k_qty, INFO, f"{sym}: ignored by request (bot {b_qty}, broker {k_qty})"))
            continue
        if b_qty and not k_qty:
            add(Difference("missing_at_broker", sym, b_qty, 0, CRITICAL,
                           f"{sym}: bot holds {b_qty} but the broker shows none"))
        elif k_qty and not b_qty:
            add(Difference("extra_at_broker", sym, 0, k_qty, WARNING,
                           f"{sym}: broker holds {k_qty} the bot does not know about "
                           "(manual trade? add to ignore_symbols if intended)"))
        elif b_qty != k_qty:
            add(Difference("qty_mismatch", sym, b_qty, k_qty, CRITICAL,
                           f"{sym}: bot holds {b_qty}, broker holds {k_qty}"))
        elif b_px and k_px and abs(k_px - b_px) / b_px * 100 > avg_price_tolerance_pct:
            add(Difference("avg_price", sym, round(b_px, 2), round(k_px, 2), INFO,
                           f"{sym}: average price differs (bot {b_px:,.2f}, broker {k_px:,.2f})"))

    for sym, qty in sorted(mis.items()):
        add(Difference("mis_open", sym, 0, qty, WARNING,
                       f"{sym}: open intraday (MIS) position of {qty}; the broker will square it off"))

    if bot_cash is not None and funds is not None:
        gap = funds.net - bot_cash
        limit = max(cash_tolerance_abs, abs(bot_cash) * cash_tolerance_pct / 100)
        sev = WARNING if abs(gap) > limit else INFO
        add(Difference("cash", "", round(bot_cash, 2), round(funds.net, 2), sev,
                       f"cash: bot Rs {bot_cash:,.2f}, broker Rs {funds.net:,.2f} (gap Rs {gap:,.2f}, "
                       f"tolerance Rs {limit:,.2f})"))
    return report


def reconcile_session(session: BrokerSession, bot_positions: Mapping[str, Any], *, bot_cash: float | None = None,
                      as_of: dt.date | str | None = None, **kwargs: Any) -> ReconcileReport:
    """Read holdings, positions and funds from a broker session and reconcile them."""
    funds = session.funds() if bot_cash is not None else None
    return reconcile(bot_positions, session.holdings(), session.positions(), bot_cash=bot_cash, funds=funds,
                     as_of=as_of, **kwargs)
