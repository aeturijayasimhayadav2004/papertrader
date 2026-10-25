"""Risk rules. Every check returns a plain-English reason, allowed or not.

Sizing:
  * fixed_pct  - spend up to max_position_pct of equity per position.
  * risk_based - size so that hitting the stop loses about risk_per_trade_pct of
                 equity (volatility sizing, as in the Turtle rules), still capped
                 by max_position_pct and free cash.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from papertrader.config import RiskConfig
from papertrader.costs import CostModel, as_cost_model


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    qty: int = 0
    max_notional: float = 0.0


class RiskManager:
    def __init__(self, cfg: RiskConfig, cost_per_side: CostModel | float):
        self.cfg = cfg
        self.costs = as_cost_model(cost_per_side)
        self.exits = cfg.exit_rules()

    def stop_price(self, entry_price: float, atr_at_signal: float | None = None) -> float:
        return self.exits.initial_stop(entry_price, atr_at_signal)

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
        atr_at_signal: float | None = None,
        orders_today: int = 0,
    ) -> RiskDecision:
        cfg = self.cfg
        if halted:
            return RiskDecision(False, halt_reason or f"Daily loss limit of {cfg.max_daily_loss_pct:g}% was hit.")
        if orders_today >= cfg.max_new_orders_per_day:
            return RiskDecision(False, f"Already placed {orders_today} new buy orders today "
                                       f"(limit {cfg.max_new_orders_per_day}).")
        used = open_positions + pending_buys
        if used >= cfg.max_open_positions:
            return RiskDecision(False, f"Already at the limit of {cfg.max_open_positions} positions "
                                       f"({open_positions} open, {pending_buys} queued to buy).")
        cap = self.max_position_value(equity)
        size_note = ""
        if cfg.sizing == "risk_based":
            per_share_risk = self.exits.stop_distance(price, atr_at_signal)
            risk_budget = equity * cfg.risk_per_trade_pct / 100.0
            risk_cap = risk_budget / per_share_risk * price if per_share_risk > 0 else cap
            if risk_cap < cap:
                cap = risk_cap
                size_note = (f" Sized so a stop-out (about Rs {per_share_risk:,.2f}/share) loses "
                             f"~{cfg.risk_per_trade_pct:g}% of equity.")
        budget = min(cap, cash_available)
        unit_cost = self.costs.buy_unit_cost(price)
        qty = math.floor(budget / unit_cost) if unit_cost > 0 else 0
        if qty < 1:
            if cash_available < unit_cost:
                return RiskDecision(False, f"Not enough free cash (Rs {cash_available:,.0f}) to buy even one share "
                                           f"at about Rs {price:,.2f}.")
            return RiskDecision(False, f"One share (Rs {price:,.2f}) costs more than the position size limit "
                                       f"(Rs {cap:,.0f}).")
        value = qty * price
        why_size = "the position size limit" if cap <= cash_available else "the free cash left"
        stop = self.stop_price(price, atr_at_signal)
        return RiskDecision(
            True,
            (f"Risk check passed: buy {qty} shares (about Rs {value:,.0f}, {value / equity:.1%} of equity, "
             f"limited by {why_size}); initial stop about Rs {stop:,.2f} "
             f"({(1 - stop / price):.1%} below today's close).{size_note}"),
            qty=qty,
            max_notional=cap,
        )
