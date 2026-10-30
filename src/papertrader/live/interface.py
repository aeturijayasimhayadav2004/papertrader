"""The broker-neutral contract every real broker adapter must implement.

A real adapter (Zerodha Kite Connect, Upstox, Dhan, Fyers, Angel One ...) wraps the
broker's official SDK or REST API behind `BrokerSession`, so the executor, the
reconciler and the preflight never depend on one broker's field names. How to write
one is in live/brokers/README.md. None exists yet, on purpose.

NSE cash-equity rules modelled here:
  * tick size 0.05 (prices must be a multiple of 5 paise)
  * lot size 1 (any whole number of shares)
  * price band / circuit limits: an order priced outside the day's band is rejected
  * order tag: at most 20 characters (brokers pass it through for SEBI algo tagging
    and for finding our own orders again after a crash)
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import ClassVar, Protocol, runtime_checkable

from papertrader.agent.types import symbol as nse_symbol

BUY, SELL = "BUY", "SELL"
SIDES = (BUY, SELL)

CNC, MIS = "CNC", "MIS"                      # delivery | intraday (auto squared off)
PRODUCTS = (CNC, MIS)

LIMIT, MARKET, SL, SL_M = "LIMIT", "MARKET", "SL", "SL-M"
ORDER_TYPES = (LIMIT, MARKET, SL, SL_M)

DAY = "DAY"
VALIDITIES = (DAY,)

OPEN, COMPLETE, REJECTED, CANCELLED, PARTIAL = "OPEN", "COMPLETE", "REJECTED", "CANCELLED", "PARTIAL"
ORDER_STATUSES = (OPEN, COMPLETE, REJECTED, CANCELLED, PARTIAL)
TERMINAL_STATUSES = (COMPLETE, REJECTED, CANCELLED)

TICK_SIZE = 0.05
LOT_SIZE = 1
MAX_TAG_LEN = 20
MAX_CLIENT_ORDER_ID_LEN = 40


class InvalidOrder(ValueError):
    """An OrderRequest that no NSE broker would accept (bad side, qty, tick, tag ...)."""


class SessionExpired(RuntimeError):
    """The broker session is missing or expired. Real brokers expire tokens daily; log in again."""


# ----- price helpers -----

def round_to_tick(price: float, tick: float = TICK_SIZE, side: str | None = None) -> float:
    """Round a price onto the NSE tick grid.

    side=None rounds to the nearest tick. side=BUY rounds DOWN and side=SELL rounds UP,
    so a protected limit price never ends up more aggressive than intended.
    """
    if price is None or not math.isfinite(price) or price <= 0:
        raise InvalidOrder(f"price must be a positive number, got {price!r}")
    steps = price / tick
    # nudge to absorb float error (e.g. 100.15 / 0.05 = 2002.9999999)
    if side == BUY:
        n = math.floor(steps + 1e-9)
    elif side == SELL:
        n = math.ceil(steps - 1e-9)
    else:
        n = math.floor(steps + 0.5)
    return round(max(n, 1) * tick, 2)


def is_on_tick(price: float, tick: float = TICK_SIZE) -> bool:
    """True when `price` is a whole multiple of the tick size (to the paisa)."""
    if price is None or not math.isfinite(price) or price <= 0:
        return False
    return abs(price / tick - round(price / tick)) < 1e-6


def within_band(price: float, lower: float | None, upper: float | None) -> bool:
    """True when `price` is inside the day's circuit band. A missing limit is no limit."""
    if lower is not None and price < lower - 1e-9:
        return False
    if upper is not None and price > upper + 1e-9:
        return False
    return True


def valid_qty(qty: int, lot: int = LOT_SIZE) -> bool:
    return isinstance(qty, int) and not isinstance(qty, bool) and qty > 0 and qty % lot == 0


# ----- records -----

@dataclass(frozen=True)
class OrderRequest:
    """One order exactly as it would go to the broker.

    symbol           NSE symbol, e.g. "INFY" (a Yahoo ticker like "INFY.NS" is converted)
    product          CNC (delivery) or MIS (intraday, squared off by the broker ~15:20)
    order_type       LIMIT, MARKET, SL (stop-limit) or SL-M (stop-market)
    price            limit price, required for LIMIT and SL
    trigger_price    required for SL and SL-M
    tag              <= 20 characters; our label on the order (SEBI algo tagging is the broker's)
    client_order_id  our idempotency key: the same id must never create a second order
    """
    symbol: str
    side: str
    qty: int
    product: str = CNC
    order_type: str = LIMIT
    price: float | None = None
    trigger_price: float | None = None
    validity: str = DAY
    tag: str = ""
    client_order_id: str = ""
    exchange: str = "NSE"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", nse_symbol(str(self.symbol)))

    def problems(self, tick: float = TICK_SIZE) -> list[str]:
        """Everything wrong with this request, in plain words. Empty list = acceptable."""
        out: list[str] = []
        if not self.symbol or not self.symbol.replace("&", "").replace("-", "").isalnum():
            out.append(f"symbol {self.symbol!r} is not an NSE symbol")
        if self.exchange != "NSE":
            out.append(f"exchange {self.exchange!r}: only NSE is supported")
        if self.side not in SIDES:
            out.append(f"side {self.side!r} must be BUY or SELL")
        if not valid_qty(self.qty):
            out.append(f"qty {self.qty!r} must be a positive whole number (lot size {LOT_SIZE})")
        if self.product not in PRODUCTS:
            out.append(f"product {self.product!r} must be CNC or MIS")
        if self.order_type not in ORDER_TYPES:
            out.append(f"order_type {self.order_type!r} must be one of {ORDER_TYPES}")
        if self.validity not in VALIDITIES:
            out.append(f"validity {self.validity!r}: only DAY is supported")
        if self.order_type in (LIMIT, SL):
            if self.price is None:
                out.append(f"{self.order_type} order needs a price")
            elif not is_on_tick(self.price, tick):
                out.append(f"price {self.price} is not a multiple of the {tick} tick size")
        elif self.price not in (None, 0):
            out.append(f"{self.order_type} order must not carry a limit price")
        if self.order_type in (SL, SL_M):
            if self.trigger_price is None:
                out.append(f"{self.order_type} order needs a trigger_price")
            elif not is_on_tick(self.trigger_price, tick):
                out.append(f"trigger_price {self.trigger_price} is not a multiple of the {tick} tick size")
        if len(self.tag) > MAX_TAG_LEN:
            out.append(f"tag {self.tag!r} is longer than {MAX_TAG_LEN} characters")
        if len(self.client_order_id) > MAX_CLIENT_ORDER_ID_LEN:
            out.append(f"client_order_id longer than {MAX_CLIENT_ORDER_ID_LEN} characters")
        return out

    def validate(self, tick: float = TICK_SIZE) -> "OrderRequest":
        issues = self.problems(tick)
        if issues:
            raise InvalidOrder("; ".join(issues))
        return self

    def notional(self, ref_price: float | None = None) -> float:
        """qty x price (the limit, else the trigger, else `ref_price`)."""
        px = self.price or self.trigger_price or ref_price or 0.0
        return self.qty * float(px)


@dataclass
class BrokerOrder:
    """The broker's view of one order. Timestamps are the exchange's (IST)."""
    order_id: str
    request: OrderRequest
    status: str = OPEN
    filled_qty: int = 0
    avg_price: float | None = None
    reject_reason: str = ""
    placed_at: dt.datetime | None = None
    exchange_time: dt.datetime | None = None          # last exchange update
    exchange_order_id: str = ""
    fees: float = 0.0
    triggered: bool = False

    @property
    def pending_qty(self) -> int:
        return 0 if self.status in TERMINAL_STATUSES else self.request.qty - self.filled_qty

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class Funds:
    """Cash in the trading account (INR). `available` is what a new order can use."""
    available: float
    used_margin: float = 0.0
    total: float | None = None

    @property
    def net(self) -> float:
        return self.total if self.total is not None else self.available + self.used_margin


@dataclass(frozen=True)
class Holding:
    """Settled delivery shares in the demat account (plus T1 shares not yet settled)."""
    symbol: str
    qty: int
    avg_price: float
    last_price: float = 0.0
    t1_qty: int = 0

    @property
    def total_qty(self) -> int:
        return self.qty + self.t1_qty


@dataclass(frozen=True)
class BrokerPosition:
    """Today's net position for one symbol and product (CNC buys show here until settled)."""
    symbol: str
    product: str
    qty: int                     # net: + long, - short (MIS only)
    avg_price: float
    last_price: float = 0.0
    realised_pnl: float = 0.0


@dataclass(frozen=True)
class Quote:
    symbol: str
    ltp: float
    bid: float | None = None
    ask: float | None = None
    upper_circuit: float | None = None
    lower_circuit: float | None = None
    timestamp: dt.datetime | None = None


@dataclass(frozen=True)
class SessionStatus:
    logged_in: bool
    expires_at: dt.datetime | None = None
    user_id: str = ""
    broker: str = ""
    note: str = ""

    def valid_at(self, now: dt.datetime) -> bool:
        return self.logged_in and (self.expires_at is None or now < self.expires_at)


@runtime_checkable
class BrokerSession(Protocol):
    """What the executor, reconciler and preflight need from a broker.

    Class attributes every adapter sets:
      name      short broker name for logs ("zerodha", "upstox", "simulated")
      verified  False until the adapter has passed the test plan in live/brokers/README.md
                against SimulatedExchange behaviours AND a full shadow month. The executor
                refuses live orders through any adapter whose class says verified = False.

    Login is deliberately not in this contract: brokers need a human for OAuth + 2FA
    every day. The adapter receives an access token obtained by that manual login.
    """
    name: ClassVar[str]
    verified: ClassVar[bool]

    def session_status(self) -> SessionStatus: ...
    def funds(self) -> Funds: ...
    def holdings(self) -> list[Holding]: ...
    def positions(self) -> list[BrokerPosition]: ...
    def orders(self) -> list[BrokerOrder]: ...
    def quote(self, symbols: list[str]) -> dict[str, Quote]: ...
    def place_order(self, request: OrderRequest) -> BrokerOrder: ...
    def cancel_order(self, order_id: str) -> BrokerOrder: ...
    def order_status(self, order_id: str) -> BrokerOrder: ...

