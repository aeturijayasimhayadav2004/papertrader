"""SimulatedExchange: an offline broker + exchange that behaves like the real thing.

It implements `BrokerSession`, so the executor, the reconciler and the preflight can
be rehearsed end to end without a broker account, a network or real money. It never
talks to anything outside this process.

Behaviours copied from real Indian brokers (each one has a test):
  * session expires every day at 06:00 IST, so a fresh login is needed each morning
  * orders only during continuous trading 09:15-15:30 IST on NSE trading days
    (papertrader.agent.market); pre-open and after-market orders are rejected
  * LIMIT fills only when the market crosses the limit (ask <= buy limit, bid >= sell limit)
  * MARKET fills at the touch plus slippage, capped at the circuit limit
  * SL / SL-M wait for the trigger, then behave as LIMIT / MARKET
  * partial fills when a quote carries less liquidity than the order needs
  * rejections: invalid request (tick size, qty, tag), outside the circuit band,
    insufficient funds, selling CNC shares you do not hold, MIS after 15:20,
    more than `max_orders_per_sec` orders in one second
  * MIS positions are squared off automatically at 15:20; open DAY orders expire at 15:30
  * a repeated client_order_id returns the existing order (idempotency)
  * charges from costs.india_delivery (CNC) and costs.india_intraday (MIS), without the
    slippage allowance (slippage here is in the fill price, not a fee)
  * CNC fills show in positions() today and move into holdings() at the next day's start

Time is injectable: pass `clock=` or call `set_time()`; every public call first runs
the day's housekeeping (expiry, square-off, settlement) for the current time.
"""
from __future__ import annotations

import datetime as dt
import itertools
from collections import deque
from dataclasses import dataclass
from typing import Callable, ClassVar

from papertrader.agent.market import CLOSE, IST, market_status, now_ist
from papertrader.costs import CostModel, india_delivery, india_intraday
from papertrader.live.interface import (BUY, CANCELLED, CNC, COMPLETE, MARKET, MIS, PARTIAL,
                                        REJECTED, SELL, SL, SL_M, TICK_SIZE, BrokerOrder, BrokerPosition, Funds,
                                        Holding, InvalidOrder, OrderRequest, Quote, SessionExpired, SessionStatus,
                                        nse_symbol, round_to_tick, within_band)

MIS_SQUARE_OFF = dt.time(15, 20)
SESSION_EXPIRES_AT = dt.time(6, 0)      # most Indian brokers expire API tokens around 06:00 IST


@dataclass
class _Pos:
    qty: int = 0
    avg: float = 0.0
    realised: float = 0.0

    def apply(self, side: str, qty: int, price: float) -> None:
        signed = qty if side == BUY else -qty
        if self.qty == 0 or (self.qty > 0) == (signed > 0):          # opening or adding
            new = self.qty + signed
            self.avg = (abs(self.qty) * self.avg + qty * price) / abs(new)
            self.qty = new
            return
        closing = min(qty, abs(self.qty))                             # reducing / flipping
        direction = 1 if self.qty > 0 else -1
        self.realised += closing * (price - self.avg) * direction
        self.qty += signed
        if self.qty == 0:
            self.avg = 0.0
        elif (self.qty > 0) != (direction > 0):                       # flipped through zero
            self.avg = price


class SimulatedExchange:
    """Offline BrokerSession. Not a real broker: `verified` is False and must stay False."""

    name: ClassVar[str] = "simulated"
    verified: ClassVar[bool] = False

    def __init__(self, cash: float = 100_000.0, *, clock: Callable[[], dt.datetime] | None = None,
                 slippage_pct: float = 0.05, max_orders_per_sec: int = 10,
                 delivery_costs: CostModel | None = None, intraday_costs: CostModel | None = None,
                 holdings: dict[str, tuple[int, float]] | None = None, user_id: str = "SIM001") -> None:
        self.cash = float(cash)
        self.clock = clock
        self.slippage = slippage_pct / 100.0
        self.max_orders_per_sec = max_orders_per_sec
        self.costs = {CNC: delivery_costs or india_delivery(slippage_pct=0.0),
                      MIS: intraday_costs or india_intraday(slippage_pct=0.0)}
        self.user_id = user_id
        self._holdings: dict[str, _Pos] = {s: _Pos(q, p) for s, (q, p) in (holdings or {}).items()}
        self._positions: dict[tuple[str, str], _Pos] = {}
        self._orders: dict[str, BrokerOrder] = {}
        self._by_client_id: dict[str, str] = {}
        self._blocked: dict[str, float] = {}           # order_id -> cash still blocked
        self._quotes: dict[str, Quote] = {}
        self._liquidity: dict[str, int | None] = {}   # qty still fillable at the current quote
        self._recent: deque[dt.datetime] = deque()
        self._ids = itertools.count(1)
        self._expires_at: dt.datetime | None = None
        self._day: dt.date | None = None
        self.place_calls = 0                           # how many times place_order was called

    # ----- clock and session -----
    @property
    def now(self) -> dt.datetime:
        return now_ist(self.clock() if self.clock else None)

    def set_time(self, when: dt.datetime) -> None:
        """Freeze the exchange clock at `when` (IST if naive) and run housekeeping."""
        fixed = now_ist(when)
        self.clock = lambda: fixed
        self._housekeep()

    def login(self, now: dt.datetime | None = None) -> SessionStatus:
        """Stand-in for the broker's manual OAuth + 2FA login. Valid till 06:00 IST tomorrow."""
        t = now_ist(now) if now else self.now
        expiry_day = t.date() + dt.timedelta(days=1) if t.time() >= SESSION_EXPIRES_AT else t.date()
        self._expires_at = dt.datetime.combine(expiry_day, SESSION_EXPIRES_AT, IST)
        self._housekeep()
        return self.session_status()

    def logout(self) -> None:
        self._expires_at = None

    def session_status(self) -> SessionStatus:
        logged_in = self._expires_at is not None and self.now < self._expires_at
        note = "" if logged_in else ("never logged in" if self._expires_at is None else "session expired; log in again")
        return SessionStatus(logged_in, self._expires_at, self.user_id, self.name, note)

    def _require_session(self) -> None:
        st = self.session_status()
        if not st.logged_in:
            raise SessionExpired(f"simulated broker: {st.note}")

    # ----- market data (the exchange feed; no session needed to set it) -----
    def set_quote(self, symbol: str, ltp: float, *, bid: float | None = None, ask: float | None = None,
                  upper: float | None = None, lower: float | None = None, liquidity: int | None = None) -> None:
        """Publish a new quote and try to match resting orders. `liquidity` caps the shares
        fillable at this quote (None = unlimited), which is how partial fills happen."""
        sym = nse_symbol(symbol)
        self._housekeep()
        self._quotes[sym] = Quote(sym, float(ltp), bid, ask, upper, lower, self.now)
        self._liquidity[sym] = liquidity
        for order in list(self._orders.values()):
            if order.request.symbol == sym and not order.is_terminal:
                self._match(order)

    # ----- BrokerSession reads -----
    def funds(self) -> Funds:
        self._require_session()
        self._housekeep()
        blocked = sum(self._blocked.values(), 0.0)
        return Funds(available=round(self.cash - blocked, 2), used_margin=round(blocked, 2), total=round(self.cash, 2))

    def holdings(self) -> list[Holding]:
        self._require_session()
        self._housekeep()
        return [Holding(s, p.qty, round(p.avg, 2), self._ltp(s)) for s, p in sorted(self._holdings.items()) if p.qty]

    def positions(self) -> list[BrokerPosition]:
        self._require_session()
        self._housekeep()
        return [BrokerPosition(s, prod, p.qty, round(p.avg, 2), self._ltp(s), round(p.realised, 2))
                for (s, prod), p in sorted(self._positions.items()) if p.qty or p.realised]

    def orders(self) -> list[BrokerOrder]:
        self._require_session()
        self._housekeep()
        return list(self._orders.values())

    def quote(self, symbols: list[str]) -> dict[str, Quote]:
        self._require_session()
        self._housekeep()
        return {s: self._quotes[s] for s in (nse_symbol(x) for x in symbols) if s in self._quotes}

    def order_status(self, order_id: str) -> BrokerOrder:
        self._require_session()
        self._housekeep()
        if order_id not in self._orders:
            raise KeyError(f"unknown order id {order_id!r}")
        return self._orders[order_id]

    # ----- BrokerSession writes -----
    def place_order(self, request: OrderRequest) -> BrokerOrder:
        self.place_calls += 1
        self._require_session()
        self._housekeep()
        if request.client_order_id and request.client_order_id in self._by_client_id:
            return self._orders[self._by_client_id[request.client_order_id]]

        now = self.now
        order = BrokerOrder(order_id=f"SIM{next(self._ids):08d}", request=request, placed_at=now,
                            exchange_time=now, exchange_order_id=f"NSE{now:%Y%m%d}{len(self._orders) + 1:06d}")
        self._orders[order.order_id] = order
        if request.client_order_id:
            self._by_client_id[request.client_order_id] = order.order_id

        while self._recent and (now - self._recent[0]).total_seconds() >= 1.0:
            self._recent.popleft()
        self._recent.append(now)
        if len(self._recent) > self.max_orders_per_sec:
            return self._reject(order, f"rate limit: more than {self.max_orders_per_sec} orders in one second")

        reason = self._entry_problem(request, now, order.order_id)
        if reason:
            return self._reject(order, reason)
        if request.side == BUY:
            self._blocked[order.order_id] = self._buy_cost_estimate(request)
        self._match(order)
        return order

    def cancel_order(self, order_id: str) -> BrokerOrder:
        order = self.order_status(order_id)
        if order.is_terminal:
            raise InvalidOrder(f"order {order_id} is already {order.status} and cannot be cancelled")
        self._finish(order, CANCELLED, "cancelled by user")
        return order

    # ----- checks -----
    def _entry_problem(self, req: OrderRequest, now: dt.datetime, order_id: str = "") -> str:
        issues = req.problems(TICK_SIZE)
        if issues:
            return "invalid order: " + "; ".join(issues)
        status = market_status(now)
        if not status.is_open:
            return f"market closed: {status.describe()}"
        if req.product == MIS and now.time() >= MIS_SQUARE_OFF:
            return f"MIS orders are not accepted after {MIS_SQUARE_OFF:%H:%M} (intraday square-off)"
        q = self._quotes.get(req.symbol)
        if q is None:
            return f"no quote for {req.symbol}"
        for label, px in (("price", req.price), ("trigger_price", req.trigger_price)):
            if px is not None and not within_band(px, q.lower_circuit, q.upper_circuit):
                return (f"{label} {px} outside the circuit band "
                        f"{q.lower_circuit if q.lower_circuit is not None else '-'}-"
                        f"{q.upper_circuit if q.upper_circuit is not None else '-'}")
        if req.side == BUY:
            need = self._buy_cost_estimate(req)
            available = self.cash - sum(self._blocked.values())
            if need > available + 1e-6:
                return f"insufficient funds: need Rs {need:,.2f}, available Rs {available:,.2f}"
        elif req.product == CNC:
            held = self._holdings.get(req.symbol, _Pos()).qty + self._positions.get((req.symbol, CNC), _Pos()).qty
            pending = sum(o.pending_qty for o in self._orders.values()
                          if o.order_id != order_id and o.request.symbol == req.symbol and o.request.side == SELL
                          and o.request.product == CNC and not o.is_terminal)
            if req.qty > held - pending:
                return f"cannot sell {req.qty} {req.symbol} CNC: only {max(held - pending, 0)} held (no CNC short selling)"
        return ""

    def _buy_cost_estimate(self, req: OrderRequest) -> float:
        q = self._quotes.get(req.symbol)
        if req.price:
            px = req.price
        elif req.trigger_price:
            px = req.trigger_price * (1 + self.slippage)
        else:
            px = ((q.ask or q.ltp) if q else 0.0) * (1 + self.slippage)
            if q and q.upper_circuit:
                px = min(px, q.upper_circuit)
        return req.qty * px + self.costs[req.product].fee(BUY, req.qty, px)

    # ----- matching -----
    def _ltp(self, sym: str) -> float:
        q = self._quotes.get(sym)
        return q.ltp if q else 0.0

    def _match(self, order: BrokerOrder) -> None:
        req, q = order.request, self._quotes.get(order.request.symbol)
        if q is None or order.is_terminal:
            return
        if req.order_type in (SL, SL_M) and not order.triggered:
            hit = q.ltp >= req.trigger_price if req.side == BUY else q.ltp <= req.trigger_price
            if not hit:
                return
            order.triggered = True
        as_market = req.order_type in (MARKET, SL_M)
        if req.side == BUY:
            touch = q.ask or q.ltp
            if as_market:
                px = round_to_tick(touch * (1 + self.slippage))
                if q.upper_circuit is not None:
                    px = min(px, q.upper_circuit)
            elif touch <= req.price + 1e-9:
                px = min(round_to_tick(touch), req.price)
            else:
                return
        else:
            touch = q.bid or q.ltp
            if as_market:
                px = round_to_tick(touch * (1 - self.slippage))
                if q.lower_circuit is not None:
                    px = max(px, q.lower_circuit)
            elif touch >= req.price - 1e-9:
                px = max(round_to_tick(touch), req.price)
            else:
                return
        liquidity = self._liquidity.get(req.symbol)
        qty = order.pending_qty if liquidity is None else min(order.pending_qty, liquidity)
        if qty <= 0:
            return
        if liquidity is not None:
            self._liquidity[req.symbol] = liquidity - qty
        self._fill(order, qty, px)

    def _fill(self, order: BrokerOrder, qty: int, px: float) -> None:
        req = order.request
        prev_filled, prev_avg, prev_fees = order.filled_qty, order.avg_price or 0.0, order.fees
        order.filled_qty = prev_filled + qty
        order.avg_price = round((prev_filled * prev_avg + qty * px) / order.filled_qty, 4)
        order.fees = round(self.costs[req.product].fee(req.side, order.filled_qty, order.avg_price), 2)
        fee_delta = order.fees - prev_fees
        if req.side == BUY:
            self.cash -= qty * px + fee_delta
            if order.order_id in self._blocked:
                per_share = self._blocked[order.order_id] / max(req.qty - prev_filled, 1)
                self._blocked[order.order_id] = max(self._blocked[order.order_id] - per_share * qty, 0.0)
        else:
            self.cash += qty * px - fee_delta
        self._positions.setdefault((req.symbol, req.product), _Pos()).apply(req.side, qty, px)
        order.exchange_time = self.now
        if order.filled_qty >= req.qty:
            self._finish(order, COMPLETE)
        else:
            order.status = PARTIAL

    def _reject(self, order: BrokerOrder, reason: str) -> BrokerOrder:
        self._finish(order, REJECTED, reason)
        return order

    def _finish(self, order: BrokerOrder, status: str, reason: str = "") -> None:
        order.status = status
        if reason:
            order.reject_reason = reason
        order.exchange_time = self.now
        self._blocked.pop(order.order_id, None)

    # ----- housekeeping: expiry, square-off, settlement -----
    def _housekeep(self) -> None:
        now = self.now
        if self._day is not None and now.date() != self._day:
            self._end_of_day("previous day ended")
            self._settle()
        self._day = now.date()
        if now.time() >= MIS_SQUARE_OFF:
            self._square_off_mis()
        if now.time() >= CLOSE:
            self._end_of_day("validity DAY expired at market close")

    def _end_of_day(self, reason: str) -> None:
        self._square_off_mis()
        for order in self._orders.values():
            if not order.is_terminal:
                self._finish(order, CANCELLED, reason)

    def _square_off_mis(self) -> None:
        for order in self._orders.values():
            if order.request.product == MIS and not order.is_terminal:
                self._finish(order, CANCELLED, "MIS order cancelled at intraday square-off")
        for (sym, prod), pos in list(self._positions.items()):
            if prod != MIS or pos.qty == 0:
                continue
            side = SELL if pos.qty > 0 else BUY
            req = OrderRequest(sym, side, abs(pos.qty), product=MIS, order_type=MARKET, tag="AUTO-SQOFF")
            order = BrokerOrder(order_id=f"SIM{next(self._ids):08d}", request=req, placed_at=self.now,
                                exchange_time=self.now)
            self._orders[order.order_id] = order
            self._fill(order, abs(pos.qty), round_to_tick(self._ltp(sym) or pos.avg))

    def _settle(self) -> None:
        """Yesterday's CNC net quantity moves into holdings; day positions reset."""
        for (sym, prod), pos in self._positions.items():
            if prod != CNC or pos.qty == 0:
                continue
            h = self._holdings.setdefault(sym, _Pos())
            h.apply(BUY if pos.qty > 0 else SELL, abs(pos.qty), pos.avg if pos.qty > 0 else h.avg)
            if h.qty == 0:
                self._holdings.pop(sym)
        self._positions = {}

