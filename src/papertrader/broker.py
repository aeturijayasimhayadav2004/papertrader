"""Simulated broker. PAPER ONLY: nothing here talks to a real exchange.

Orders are queued at a day's close and filled at the next trading day's open.
Charges come from papertrader.costs. Stops, trailing stops and take-profit
targets follow papertrader.exits, the same rules the backtester uses.

`BrokerAPI` is the interface the runner relies on. A live adapter (see
docs/ARCHITECTURE.md) must implement the same methods so strategies, risk and
the runner do not change between paper and live.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from papertrader.costs import CostModel, as_cost_model
from papertrader.exits import ExitRules, intraday_exit

BUY, SELL = "BUY", "SELL"
PENDING, FILLED, CANCELLED = "PENDING", "FILLED", "CANCELLED"


class BrokerError(ValueError):
    pass


def _d(value: Any) -> dt.date | None:
    if value is None or isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


@dataclass
class Order:
    order_id: str
    created_date: dt.date
    ticker: str
    side: str
    qty: int
    strategy: str
    reason: str
    status: str = PENDING
    max_notional: float | None = None
    fill_date: dt.date | None = None
    fill_price: float | None = None
    fees: float = 0.0
    note: str = ""
    atr_at_signal: float | None = None  # for ATR stops: the ATR known at the signal close


@dataclass
class Position:
    ticker: str
    qty: int
    entry_date: dt.date
    entry_price: float
    entry_fees: float
    stop_price: float
    strategy: str
    last_price: float
    highest_close: float = 0.0
    target_price: float | None = None

    @property
    def market_value(self) -> float:
        return self.qty * self.last_price

    @property
    def unrealized_pnl(self) -> float:
        """Mark-to-market P&L after the entry fee (exit fee not yet paid)."""
        return self.qty * (self.last_price - self.entry_price) - self.entry_fees


@dataclass
class ClosedTrade:
    trade_id: str
    ticker: str
    strategy: str
    entry_date: dt.date
    entry_price: float
    exit_date: dt.date
    exit_price: float
    qty: int
    gross_pnl: float
    fees: float
    net_pnl: float
    return_pct: float
    exit_reason: str


class BrokerAPI(Protocol):
    """What the runner needs from any broker, paper or live."""
    cash: float
    positions: dict[str, Position]

    @property
    def pending_orders(self) -> list[Order]: ...
    def pending_for(self, ticker: str) -> list[Order]: ...
    def equity(self) -> float: ...
    def reserved_cash(self) -> float: ...
    def place_order(self, created_date: dt.date, ticker: str, side: str, qty: int, strategy: str,
                    reason: str, max_notional: float | None = None,
                    atr_at_signal: float | None = None) -> Order: ...
    def mark(self, closes: dict[str, float]) -> None: ...
    def drain_touched(self) -> list[Order]: ...


@dataclass
class PaperBroker:
    cash: float
    cost_per_side: CostModel | float
    stop_loss_pct: float = 5.0
    exits: ExitRules | None = None
    positions: dict[str, Position] = field(default_factory=dict)
    orders: list[Order] = field(default_factory=list)  # history for this session + carried pending orders
    trades: list[ClosedTrade] = field(default_factory=list)
    _touched: dict[str, Order] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.costs = as_cost_model(self.cost_per_side)
        if self.exits is None:
            self.exits = ExitRules(stop_loss_pct=self.stop_loss_pct)

    # ----- queries -----
    @property
    def pending_orders(self) -> list[Order]:
        return [o for o in self.orders if o.status == PENDING]

    def pending_for(self, ticker: str) -> list[Order]:
        return [o for o in self.pending_orders if o.ticker == ticker]

    def positions_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    def equity(self) -> float:
        return self.cash + self.positions_value()

    def reserved_cash(self) -> float:
        """Estimated cash needed for queued buys (max_notional, or qty at last price)."""
        return sum((o.max_notional or 0.0) for o in self.pending_orders if o.side == BUY)

    # ----- orders -----
    def place_order(self, created_date: dt.date, ticker: str, side: str, qty: int, strategy: str,
                    reason: str, max_notional: float | None = None,
                    atr_at_signal: float | None = None) -> Order:
        if side not in (BUY, SELL):
            raise BrokerError(f"unknown side {side!r}")
        if qty < 1:
            raise BrokerError("quantity must be at least 1")
        if any(o.side == side for o in self.pending_for(ticker)):
            raise BrokerError(f"a {side} order for {ticker} is already queued")
        if side == BUY and ticker in self.positions:
            raise BrokerError(f"already holding {ticker}")
        if side == SELL:
            pos = self.positions.get(ticker)
            if pos is None:
                raise BrokerError(f"cannot sell {ticker}: not held")
            qty = min(qty, pos.qty)
        order = Order(f"{created_date:%Y%m%d}-{ticker}-{side}", created_date, ticker, side, int(qty),
                      strategy, reason, max_notional=max_notional, atr_at_signal=atr_at_signal)
        self.orders.append(order)
        self._touch(order)
        return order

    def fill_pending(self, day: dt.date, opens: dict[str, float]) -> list[Order]:
        """Fill orders created before `day` at `day`'s open. Sells first to free cash."""
        done = []
        for order in sorted(self.pending_orders, key=lambda o: o.side != SELL):
            if order.created_date >= day or order.ticker not in opens:
                continue  # same-day fills are not allowed; no price today -> stays queued
            price = float(opens[order.ticker])
            if order.side == SELL:
                self._fill_sell(order, day, price)
            else:
                self._fill_buy(order, day, price)
            done.append(order)
        return done

    def _fill_buy(self, order: Order, day: dt.date, price: float) -> None:
        unit = self.costs.buy_unit_cost(price)
        qty = order.qty
        if order.max_notional:
            qty = min(qty, math.floor(order.max_notional / unit))
        qty = min(qty, math.floor(self.cash / unit))
        if order.ticker in self.positions or qty < 1:
            why = "already holding" if order.ticker in self.positions else "not enough cash at the open"
            self._finish(order, CANCELLED, day, note=f"Cancelled: {why}.")
            return
        fees = self.costs.fee(BUY, qty, price)
        self.cash -= qty * price + fees
        note = "" if qty == order.qty else f"Quantity cut from {order.qty} to {qty} to respect cash / size limits."
        order.qty = qty
        self.positions[order.ticker] = Position(
            order.ticker, qty, day, price, fees, self.exits.initial_stop(price, order.atr_at_signal),
            order.strategy, price, highest_close=price, target_price=self.exits.target(price, order.atr_at_signal),
        )
        self._finish(order, FILLED, day, price, fees, note)

    def _fill_sell(self, order: Order, day: dt.date, price: float) -> None:
        pos = self.positions.get(order.ticker)
        if pos is None:
            self._finish(order, CANCELLED, day, note="Cancelled: position no longer held.")
            return
        trade = self._close(pos, day, price, "signal")
        order.qty = pos.qty
        self._finish(order, FILLED, day, price, trade.fees - pos.entry_fees)

    def check_stops(self, day: dt.date, opens: dict[str, float], lows: dict[str, float]) -> list[ClosedTrade]:
        return self.check_exits(day, opens, {}, lows)

    def check_exits(self, day: dt.date, opens: dict[str, float], highs: dict[str, float],
                    lows: dict[str, float]) -> list[ClosedTrade]:
        """Stop-loss / take-profit hits during `day` (see papertrader.exits)."""
        out = []
        for ticker, pos in list(self.positions.items()):
            if ticker not in lows:
                continue
            open_ = float(opens.get(ticker, pos.stop_price))
            hit = intraday_exit(open_, float(highs.get(ticker, 0.0)), float(lows[ticker]), pos.stop_price,
                                pos.target_price if ticker in highs else None)
            if hit is None:
                continue
            price, reason = hit
            trade = self._close(pos, day, price, reason)
            if reason == "stop_loss":
                tag, why = "STOP", f"Stop-loss: price fell to the stop level of Rs {pos.stop_price:,.2f}."
                note = "Gapped below the stop; sold at the open." if price < pos.stop_price else ""
            else:
                tag, why = "TARGET", f"Take-profit: price reached the target of Rs {pos.target_price:,.2f}."
                note = "Gapped above the target; sold at the open." if price > pos.target_price else ""
            order = Order(f"{day:%Y%m%d}-{ticker}-{tag}", day, ticker, SELL, pos.qty, pos.strategy, why)
            self.orders.append(order)
            self._finish(order, FILLED, day, price, trade.fees - pos.entry_fees, note)
            out.append(trade)
        return out

    def mark(self, closes: dict[str, float]) -> None:
        """Mark positions at the close and trail stops (the new stop applies from the next day)."""
        for ticker, pos in self.positions.items():
            if ticker in closes:
                pos.last_price = float(closes[ticker])
                pos.highest_close = max(pos.highest_close or pos.entry_price, pos.last_price)
                pos.stop_price = self.exits.trail(pos.stop_price, pos.highest_close)

    def _close(self, pos: Position, day: dt.date, price: float, reason: str) -> ClosedTrade:
        exit_fee = self.costs.fee(SELL, pos.qty, price)
        self.cash += pos.qty * price - exit_fee
        gross = pos.qty * (price - pos.entry_price)
        fees = pos.entry_fees + exit_fee
        cost_basis = pos.qty * pos.entry_price + pos.entry_fees
        trade = ClosedTrade(f"{pos.ticker}|{pos.entry_date}|{day}", pos.ticker, pos.strategy, pos.entry_date,
                            pos.entry_price, day, price, pos.qty, gross, fees, gross - fees,
                            (gross - fees) / cost_basis, reason)
        del self.positions[pos.ticker]
        self.trades.append(trade)
        return trade

    def _finish(self, order: Order, status: str, day: dt.date, price: float | None = None,
                fees: float = 0.0, note: str = "") -> None:
        order.status, order.fill_date, order.fill_price, order.fees = status, day, price, fees
        order.note = note or order.note
        self._touch(order)

    def _touch(self, order: Order) -> None:
        self._touched[order.order_id] = order

    def drain_touched(self) -> list[Order]:
        """Orders created or changed since the last call (for persisting)."""
        out = list(self._touched.values())
        self._touched.clear()
        return out

    # ----- persistence -----
    def to_dict(self) -> dict[str, Any]:
        def ser(obj) -> dict[str, Any]:
            return {k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in asdict(obj).items()}

        return {
            "cash": self.cash,
            "positions": [ser(p) for p in self.positions.values()],
            "pending_orders": [ser(o) for o in self.pending_orders],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], cost_per_side: CostModel | float, stop_loss_pct: float,
                  exits: ExitRules | None = None) -> "PaperBroker":
        b = cls(float(data["cash"]), cost_per_side, stop_loss_pct, exits)
        for p in data.get("positions", []):
            p = {**p, "entry_date": _d(p["entry_date"])}
            b.positions[p["ticker"]] = Position(**p)
        for o in data.get("pending_orders", []):
            o = {**o, "created_date": _d(o["created_date"]), "fill_date": _d(o.get("fill_date"))}
            b.orders.append(Order(**o))
        return b
