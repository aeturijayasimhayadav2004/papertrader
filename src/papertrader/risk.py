"""Risk rules. Every check returns a plain-English reason, allowed or not."""
from __future__ import annotations

import math
from dataclasses import dataclass

from papertrader.config import RiskConfig


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    qty: int = 0
    max_notional: float = 0.0


class RiskManager:
    def __init__(self, cfg: RiskConfig, cost_per_side: float):
        self.cfg = cfg
        self.cost = cost_per_side

    def stop_price(self, entry_price: float) -> float:
        return entry_price * (1.0 - self.cfg.stop_loss_pct / 100.0)

    def max_position_value(self, equity: float) -> float:
        return equity * self.cfg.max_position_pct / 100.0

    def daily_loss(self, prev_equity: float, equity: float) -> tuple[bool, float, str]:
        """(halted, daily change as a fraction, reason). Halted = no new buys for the rest of the day."""
        change = equity / prev_equity - 1.0 if prev_equity > 0 else 0.0
        limit = self.cfg.max_daily_loss_pct
        if change <= -limit / 100.0:
            return True, change, (f"Equity fell {abs(change):.2%} today, hitting the {limit:g}% daily loss limit: "
                                  "no new buys until the next trading day.")
        return False, change, ""

    def check_new_position(
        self,
        *,
        price: float,
        equity: float,
        cash_available: float,
        open_positions: int,
        pending_buys: int,
        halted: bool,
        halt_reason: str = "",
    ) -> RiskDecision:
        cfg = self.cfg
        if halted:
            return RiskDecision(False, halt_reason or f"Daily loss limit of {cfg.max_daily_loss_pct:g}% was hit.")
        used = open_positions + pending_buys
        if used >= cfg.max_open_positions:
            return RiskDecision(False, f"Already at the limit of {cfg.max_open_positions} positions "
                                       f"({open_positions} open, {pending_buys} queued to buy).")
        cap = self.max_position_value(equity)
        budget = min(cap, cash_available)
        unit_cost = price * (1.0 + self.cost)
        qty = math.floor(budget / unit_cost) if unit_cost > 0 else 0
        if qty < 1:
            if cash_available < unit_cost:
                return RiskDecision(False, f"Not enough free cash (Rs {cash_available:,.0f}) to buy even one share "
                                           f"at about Rs {price:,.2f}.")
            return RiskDecision(False, f"One share (Rs {price:,.2f}) costs more than the {cfg.max_position_pct:g}% "
                                       f"per-position cap (Rs {cap:,.0f}).")
        value = qty * price
        why_size = "the per-position cap" if cap <= cash_available else "the free cash left"
        return RiskDecision(
            True,
            (f"Risk check passed: buy {qty} shares (about Rs {value:,.0f}, {value / equity:.1%} of equity, "
             f"limited by {why_size}); stop-loss {cfg.stop_loss_pct:g}% below the fill price."),
            qty=qty,
            max_notional=cap,
        )
