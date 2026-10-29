"""Transaction cost models.

`flat`: one percentage per side (brokerage + taxes + slippage lumped together).
`india_delivery`: NSE equity delivery charges itemised the way Indian contract
notes show them (STT, exchange fee, SEBI fee, stamp duty, GST, DP charge), plus
a slippage allowance.
`india_intraday`: NSE equity intraday (MIS) charges: brokerage 0.03% capped at
Rs 20 per order, STT 0.025% on the sell only, stamp duty 0.003% on the buy
only, no DP charge. Rates are config values because they change with budgets
and broker plans; check your broker's charges page before going live.

Fill prices are never adjusted: slippage is charged as a cost, so the backtester
and the paper broker stay on the same price (the next open).
"""
from __future__ import annotations

from dataclasses import dataclass

BUY, SELL = "BUY", "SELL"


@dataclass(frozen=True)
class CostModel:
    buy_rate: float           # fraction of notional on a buy
    sell_rate: float          # fraction of notional on a sell
    sell_fixed: float = 0.0   # flat INR per sell order (DP charge)
    brokerage_rate: float = 0.0
    brokerage_cap: float | None = None  # max brokerage per order (INR)
    gst: float = 0.0          # GST on brokerage (already in the rates for the other GST-able fees)
    label: str = "flat"

    def fee(self, side: str, qty: float, price: float) -> float:
        """Total charges in INR for one fill."""
        notional = qty * price
        if notional <= 0:
            return 0.0
        rate = self.buy_rate if side == BUY else self.sell_rate
        brokerage = notional * self.brokerage_rate
        if self.brokerage_cap is not None:
            brokerage = min(brokerage, self.brokerage_cap)
        fee = notional * rate + brokerage * (1.0 + self.gst)
        return fee + (self.sell_fixed if side == SELL else 0.0)

    def buy_unit_cost(self, price: float) -> float:
        """Upper bound on the cash one share costs, for sizing (ignores the brokerage cap)."""
        return price * (1.0 + self.buy_rate + self.brokerage_rate * (1.0 + self.gst))

    def max_qty(self, cash: float, price: float) -> float:
        """Largest (fractional) quantity whose price plus buy charges fits in `cash`."""
        unit = self.buy_unit_cost(price)
        return max(cash / unit, 0.0) if unit > 0 else 0.0

    def round_trip_pct(self, notional: float = 100_000.0) -> float:
        """Buy + sell charges as a fraction of `notional` (for reports)."""
        return (self.fee(BUY, notional, 1.0) + self.fee(SELL, notional, 1.0)) / notional


def flat(per_side: float) -> CostModel:
    return CostModel(per_side, per_side, label=f"flat {per_side * 100:g}% per side")


def india_delivery(
    *,
    brokerage_pct: float = 0.0,
    brokerage_cap: float | None = 20.0,
    stt_pct: float = 0.1,
    exchange_pct: float = 0.00297,
    sebi_per_crore: float = 10.0,
    stamp_buy_pct: float = 0.015,
    gst_pct: float = 18.0,
    dp_charge_per_sell: float = 15.93,
    slippage_pct: float = 0.05,
) -> CostModel:
    gst = gst_pct / 100.0
    exch = exchange_pct / 100.0
    sebi = sebi_per_crore / 1e7
    common = stt_pct / 100.0 + (exch + sebi) * (1.0 + gst) + slippage_pct / 100.0
    return CostModel(
        buy_rate=common + stamp_buy_pct / 100.0,
        sell_rate=common,
        sell_fixed=dp_charge_per_sell,
        brokerage_rate=brokerage_pct / 100.0,
        brokerage_cap=brokerage_cap,
        gst=gst,
        label="NSE delivery (itemised)",
    )


def india_intraday(
    *,
    brokerage_pct: float = 0.03,
    brokerage_cap: float | None = 20.0,
    stt_sell_pct: float = 0.025,
    exchange_pct: float = 0.00297,
    sebi_per_crore: float = 10.0,
    stamp_buy_pct: float = 0.003,
    gst_pct: float = 18.0,
    slippage_pct: float = 0.05,
) -> CostModel:
    """NSE equity intraday (MIS). STT and stamp duty are one-sided, so the buy and sell
    rates differ; GST (18%) applies to brokerage, exchange and SEBI fees. No DP charge
    because nothing is delivered to the demat account."""
    gst = gst_pct / 100.0
    common = (exchange_pct / 100.0 + sebi_per_crore / 1e7) * (1.0 + gst) + slippage_pct / 100.0
    return CostModel(
        buy_rate=common + stamp_buy_pct / 100.0,
        sell_rate=common + stt_sell_pct / 100.0,
        brokerage_rate=brokerage_pct / 100.0,
        brokerage_cap=brokerage_cap,
        gst=gst,
        label="NSE intraday MIS (itemised)",
    )


def as_cost_model(cost: "CostModel | float") -> CostModel:
    """Accept a CostModel or a flat per-side fraction (older call sites and tests)."""
    return cost if isinstance(cost, CostModel) else flat(float(cost))
