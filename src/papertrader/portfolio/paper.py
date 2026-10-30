"""Paper trading for one rotation strategy: its own account, separate from the per-stock bot.

Same rules as the research backtest (portfolio/backtest.py), applied one day at a
time so the result can be compared with it later:

  * decide at the close of the last trading day of the month (NSE calendar),
    using only bars up to that close;
  * orders fill at the next trading day's open, sells first, with NSE delivery
    charges; a buy is cut down to the cash available; a stock with no bar that
    day keeps its order lapsed until the next rebalance;
  * holdings are marked at every close.

State lives in the `rotation_snapshots` table (one row per day, full state in
state_json), so each run picks up where the last one stopped and catches up
missed days in order. PAPER MONEY ONLY.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from papertrader.agent import market
from papertrader.config import Config
from papertrader.costs import BUY, SELL, CostModel
from papertrader.data import load_prices
from papertrader.portfolio.backtest import rebalance_orders
from papertrader.portfolio.rotation import build_rotation
from papertrader.storage import Store, upsert

log = logging.getLogger(__name__)

TABLE = "rotation_snapshots"


@dataclass(frozen=True)
class PortfolioConfig:
    enabled: bool = False
    strategy: str = "momentum_12_1_regime"
    universe: str = "nifty100"           # config | nifty50 | nifty100 | nifty500
    capital: float = 1_000_000.0
    rebalance_band_pct: float = 1.0      # skip top-ups / trims smaller than this % of equity
    history_days: int = 500              # daily bars to load (12-1 momentum needs ~13 months)
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "PortfolioConfig":
        d = dict(d or {})
        unknown = set(d) - {f.name for f in dataclasses.fields(cls)}
        if unknown:
            raise ValueError(f"portfolio: unknown key(s) {sorted(unknown)}")
        return cls(**d)


def is_rebalance_day(d: dt.date) -> bool:
    """Last NSE trading day of the month."""
    return market.is_trading_day(d) and market.next_trading_day(d).month != d.month


@dataclass
class Account:
    cash: float
    holdings: dict[str, int] = field(default_factory=dict)
    pending: list[list] = field(default_factory=list)   # [ticker, side, qty] for the next open
    last_rebalance: str | None = None
    fees_paid: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Account":
        return cls(float(d["cash"]), {k: int(v) for k, v in (d.get("holdings") or {}).items()},
                   [list(x) for x in d.get("pending") or []], d.get("last_rebalance"),
                   float(d.get("fees_paid") or 0.0))


def fill_pending(acct: Account, opens: dict[str, float], costs: CostModel, day: dt.date) -> list[dict]:
    """Fill queued orders at today's opens (sells first, as queued). Returns fill records."""
    fills = []
    for ticker, side, qty in acct.pending:
        px = opens.get(ticker)
        if px is None or not math.isfinite(px) or px <= 0:
            fills.append({"date": day.isoformat(), "ticker": ticker, "side": side, "qty": 0, "price": None,
                          "fee": 0.0, "note": "no price today: order lapsed"})
            continue
        if side == SELL:
            qty = min(int(qty), acct.holdings.get(ticker, 0))
            if qty <= 0:
                continue
            fee = costs.fee(SELL, qty, px)
            acct.cash += qty * px - fee
            acct.holdings[ticker] -= qty
            if acct.holdings[ticker] == 0:
                del acct.holdings[ticker]
        else:
            qty = min(int(qty), int(math.floor(max(acct.cash, 0.0) / costs.buy_unit_cost(px))))
            if qty <= 0:
                continue
            fee = costs.fee(BUY, qty, px)
            acct.cash -= qty * px + fee
            acct.holdings[ticker] = acct.holdings.get(ticker, 0) + qty
        acct.fees_paid += fee
        fills.append({"date": day.isoformat(), "ticker": ticker, "side": side, "qty": qty, "price": round(px, 2),
                      "fee": round(fee, 2), "note": "rebalance"})
    acct.pending = []
    return fills


def _row(df: pd.DataFrame, day: pd.Timestamp, col: str) -> float | None:
    if day in df.index:
        v = float(df.at[day, col])
        return v if math.isfinite(v) and v > 0 else None
    return None


class RotationPaper:
    def __init__(self, cfg: Config, store: Store, price_loader: Callable[..., Any] = load_prices,
                 universe_loader: Callable[[str], list[str]] | None = None, now: dt.datetime | None = None):
        self.cfg = cfg
        self.pcfg = PortfolioConfig.from_dict(cfg.sections.get("portfolio"))
        self.store = store
        self.price_loader = price_loader
        self.universe_loader = universe_loader
        self.now = market.now_ist(now)
        self.costs = cfg.costs.model()
        self.strategy = build_rotation(self.pcfg.strategy, self.pcfg.params)

    def _tickers(self) -> list[str]:
        if self.pcfg.universe == "config":
            return list(self.cfg.universe)
        if self.universe_loader is not None:
            return self.universe_loader(self.pcfg.universe)
        from papertrader.agent.universe import load_universe
        return load_universe(self.pcfg.universe, cache_dir=self.cfg.data.cache_dir / "universe")

    def _prices(self) -> tuple[dict[str, pd.DataFrame], pd.DataFrame | None]:
        start = self.now.date() - dt.timedelta(days=self.pcfg.history_days)
        data_cfg = dataclasses.replace(self.cfg.data, start=start, cache_dir=self.cfg.data.cache_dir / "rotation")
        tickers = list(dict.fromkeys(self._tickers() + [self.cfg.benchmark]))
        prices, warnings = self.price_loader(tickers, data_cfg, now=self.now)
        if warnings:
            log.info("Rotation data: %d warning(s), e.g. %s", len(warnings), warnings[0])
        bench = prices.pop(self.cfg.benchmark, None)
        return prices, bench

    def _latest(self) -> tuple[dt.date, Account] | None:
        rows = self.store.sqlite.rows(TABLE, order_by="as_of_date")
        remote = None
        if self.store.remote is not None:
            try:
                data = (self.store.remote.client.table(TABLE).select("as_of_date,state_json")
                        .order("as_of_date", desc=True).limit(1).execute().data)
                remote = data[0] if data else None
            except Exception as exc:
                log.warning("Rotation state not read from Supabase: %s", exc)
        cands = []
        if rows and rows[-1].get("state_json"):
            import json
            st = rows[-1]["state_json"]
            cands.append((dt.date.fromisoformat(rows[-1]["as_of_date"]), json.loads(st) if isinstance(st, str) else st))
        if remote and remote.get("state_json"):
            cands.append((dt.date.fromisoformat(str(remote["as_of_date"])[:10]), remote["state_json"]))
        if not cands:
            return None
        d, st = max(cands, key=lambda c: c[0])
        return d, Account.from_dict(st)

    def run(self) -> list[str]:
        """Process every new completed trading day. Returns one-line events for the summary."""
        prices, bench = self._prices()
        if not prices:
            raise RuntimeError("no price data for the rotation universe")
        cal = sorted(set().union(*(df.index for df in prices.values())))
        saved = self._latest()
        if saved is None:
            days = cal[-1:]
            acct = Account(cash=self.pcfg.capital)
            log.info("Rotation paper account: starting with Rs %s cash.", f"{self.pcfg.capital:,.0f}")
        else:
            last, acct = saved
            days = [d for d in cal if d.date() > last]
        events: list[str] = []
        for day in days:
            events += self._process(day, prices, bench, acct)
        return events

    def _process(self, day: pd.Timestamp, prices: dict[str, pd.DataFrame], bench: pd.DataFrame | None,
                 acct: Account) -> list[str]:
        d = day.date()
        events = []
        opens = {t: v for t, df in prices.items() if (v := _row(df, day, "Open")) is not None}
        fills = fill_pending(acct, opens, self.costs, d)
        done = [f for f in fills if f["qty"]]
        if done:
            events.append(f"{d} rotation: {len(done)} fill(s) at the open, charges Rs "
                          f"{sum(f['fee'] for f in done):,.0f}.")

        closes = {}
        for t, df in prices.items():
            h = df.loc[:day, "Close"]
            if len(h):
                closes[t] = float(h.iloc[-1])
        held_value = sum(q * closes.get(t, 0.0) for t, q in acct.holdings.items())
        equity = acct.cash + held_value

        reasons: dict[str, str] = {}
        note = ""
        if is_rebalance_day(d):
            hist = {t: df.loc[:day] for t, df in prices.items()}
            bh = bench.loc[:day] if bench is not None else None
            dec = self.strategy.decide(hist, day, bh)
            reasons, note = dec.reasons, dec.note
            orders = rebalance_orders(dec.weights, acct.holdings, closes, equity, self.costs,
                                      self.pcfg.rebalance_band_pct / 100.0 * equity)
            acct.pending = [list(o) for o in orders]
            acct.last_rebalance = d.isoformat()
            picks = ", ".join(t.removesuffix(".NS") for t in sorted(dec.weights)) or "none (all cash)"
            events.append(f"{d} rotation rebalance ({self.strategy.label}): holdings -> {picks}; "
                          f"{len(orders)} order(s) for the next open. {note}".strip())

        bclose = _row(bench, day, "Close") if bench is not None else None
        holdings = [{"ticker": t, "qty": q, "price": closes.get(t), "value": round(q * closes.get(t, 0.0), 2),
                     "weight": round(q * closes.get(t, 0.0) / equity, 4) if equity else 0.0,
                     "reason": reasons.get(t, "")} for t, q in sorted(acct.holdings.items())]
        self.store.write([upsert(TABLE, [{
            "as_of_date": d, "strategy": self.strategy.name, "label": self.strategy.label,
            "cash": round(acct.cash, 2), "holdings_value": round(held_value, 2), "equity": round(equity, 2),
            "benchmark_close": bclose, "n_holdings": len(acct.holdings), "holdings": holdings,
            "pending": acct.pending, "fills": fills, "note": note, "state_json": acct.to_dict(),
            "updated_at": dt.datetime.now(dt.timezone.utc),
        }])])
        log.info("Rotation %s: equity Rs %s, %d holdings, %d pending.", d, f"{equity:,.0f}", len(acct.holdings),
                 len(acct.pending))
        return events
