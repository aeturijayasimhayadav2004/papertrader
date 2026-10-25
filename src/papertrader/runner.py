"""Daily pipeline. One call processes every completed trading day not yet processed.

For each new trading day D (in order):
  1. Re-rank strategies if due (backtests use data up to D only).
  2. Fill orders queued at the previous close at D's open.
  3. Check stop-losses against D's low.
  4. Mark positions at D's close; check the daily loss limit.
  5. Ask every strategy about every stock at D's close; apply risk rules;
     queue orders for the next open. Every decision is logged with its reason.
  6. Save decisions, orders, trades, positions and a snapshot (with full state).

Re-running is safe: state is loaded from the latest saved snapshot, so a day that
was already processed is skipped (or, with force=True, recomputed from the
snapshot before it and overwritten by primary key, never duplicated).
"""
from __future__ import annotations

import datetime as dt
import logging
import platform
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from papertrader.broker import BUY, SELL, BrokerError, PaperBroker
from papertrader.config import Config
from papertrader.data import DataError, load_prices
from papertrader.risk import RiskManager
from papertrader.selection import Selection, evaluate_strategies
from papertrader.storage import StateUnavailableError, Store, WriteOp, replace, upsert
from papertrader.strategies import Signal, Strategy, build_strategies

log = logging.getLogger("papertrader.runner")

# Actions recorded for each (stock, strategy) decision.
A_BUY, A_SELL, A_HOLD = "BUY", "SELL", "HOLD"
A_BLOCKED, A_NOT_SELECTED, A_NO_DATA = "BLOCKED_BY_RISK", "NOT_SELECTED", "NO_DATA"

PriceLoader = Callable[..., tuple[dict[str, pd.DataFrame], list[str]]]
Selector = Callable[[list[Strategy], dict[str, pd.DataFrame], Config, dt.date], Selection]


@dataclass
class RunSummary:
    run_id: str
    status: str  # ok | noop | aborted | failed
    message: str
    as_of_date: dt.date | None = None
    days_processed: list[dt.date] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)


class DailyRunner:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        strategies: list[Strategy] | None = None,
        price_loader: PriceLoader = load_prices,
        selector: Selector = evaluate_strategies,
        now: dt.datetime | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.strategies = strategies if strategies is not None else build_strategies(cfg.strategies)
        self.by_name = {s.name: s for s in self.strategies}
        self.price_loader = price_loader
        self.selector = selector
        self.tz = ZoneInfo(cfg.data.market_tz)
        self.now = now.astimezone(self.tz) if now else dt.datetime.now(self.tz)
        self.risk = RiskManager(cfg.risk, cfg.costs.per_side)

    # ------------------------------------------------------------------ run
    def run(self, force: bool = False, refresh: bool = False) -> RunSummary:
        run_id = f"{self.now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        started = dt.datetime.now(dt.timezone.utc)
        summary = RunSummary(run_id, "failed", "")
        log.info("=== PAPER TRADING run %s (no real money) ===", run_id)
        try:
            self._write_run(run_id, started, None, "running", None, 0, "Run started.")
            self._run(summary, force, refresh)
        except StateUnavailableError as exc:
            summary.status, summary.message = "aborted", str(exc)
            log.error("Run aborted, no trades made: %s", exc)
        except Exception as exc:  # keep a record of every failure
            summary.status, summary.message = "failed", f"{type(exc).__name__}: {exc}"
            log.exception("Run failed")
        finally:
            sync_err = self.store.sync(force=True)
            if sync_err and self.store.remote_wanted:
                summary.message += f" Supabase not updated yet ({self.store.pending_sync()} batches queued): {sync_err}"
            try:
                self._write_run(run_id, started, dt.datetime.now(dt.timezone.utc), summary.status,
                                summary.as_of_date, len(summary.days_processed), summary.message)
                self.store.sync(force=True)
            except Exception:
                log.exception("Could not record the run")
        log.info("Run %s finished: %s - %s", run_id, summary.status.upper(), summary.message)
        return summary

    def _write_run(self, run_id, started, finished, status, as_of, days, message) -> None:
        self.store.write([upsert("runs", [{
            "run_id": run_id, "started_at": started, "finished_at": finished, "status": status,
            "as_of_date": as_of, "days_processed": days, "message": message[:2000], "host": platform.node(),
        }])])

    def _run(self, summary: RunSummary, force: bool, refresh: bool) -> None:
        cfg = self.cfg
        tickers = list(dict.fromkeys(cfg.universe + [cfg.benchmark]))
        prices, warnings = self.price_loader(tickers, cfg.data, refresh=refresh, now=self.now)
        universe = {t: prices[t] for t in cfg.universe if t in prices}
        if not universe:
            raise DataError("No price data for any stock in the universe: " + "; ".join(warnings))
        bench = prices.get(cfg.benchmark)

        calendar = sorted(set().union(*(df.index for df in universe.values())))
        last_bar = calendar[-1].date()
        saved = self.store.load_latest_state(before=last_bar if force else None)

        if saved is None:
            days = [calendar[-1]]
            broker = PaperBroker(cfg.capital, cfg.costs.per_side, cfg.risk.stop_loss_pct)
            selection_state, prev_equity = None, cfg.capital
            log.info("No saved state: starting a fresh paper account with Rs %s.", f"{cfg.capital:,.0f}")
        else:
            saved_date, state = saved
            days = [d for d in calendar if saved_date < d.date() <= last_bar]
            broker = PaperBroker.from_dict(state["broker"], cfg.costs.per_side, cfg.risk.stop_loss_pct)
            selection_state, prev_equity = state.get("selection"), float(state["equity"])
            log.info("Loaded state as of %s (equity Rs %s).", saved_date, f"{prev_equity:,.0f}")

        summary.as_of_date = last_bar
        if not days:
            summary.status = "noop"
            summary.message = f"Already processed the latest completed trading day ({last_bar}); nothing to do."
            return

        for day in days:
            ops, selection_state, prev_equity, decisions = self._process_day(
                day, universe, bench, broker, selection_state, prev_equity)
            self.store.write(ops)
            summary.days_processed.append(day.date())
            summary.decisions = decisions

        n_trades = sum(1 for d in summary.decisions if d["action"] in (A_BUY, A_SELL))
        summary.status = "ok"
        summary.message = (f"Processed {len(days)} trading day(s) up to {last_bar}. "
                           f"Equity Rs {prev_equity:,.0f}. {n_trades} order(s) queued for the next open. "
                           + (f"Data warnings: {'; '.join(warnings)}" if warnings else ""))

    # ------------------------------------------------------------- one day
    def _process_day(self, day: pd.Timestamp, universe: dict[str, pd.DataFrame], bench: pd.DataFrame | None,
                     broker: PaperBroker, selection_state: dict | None, prev_equity: float):
        cfg, d = self.cfg, day.date()
        log.info("--- Processing %s ---", d)
        history = {t: df.loc[:day] for t, df in universe.items()}
        today = {t: df.loc[day] for t, df in universe.items() if day in df.index}
        ops: list[WriteOp] = []

        # 1) strategy selection
        if self._rerank_due(selection_state, d):
            selection = self.selector(self.strategies, history, cfg, d)
            selection_state = selection.to_state()
            ops.append(upsert("backtest_results", selection.to_rows()))
            log.info("Strategy selection re-ranked: %s", selection.summary())
        selected: list[str] = selection_state["selected"]

        # 2) fills at today's open, 3) stops
        for o in broker.fill_pending(d, {t: bar["Open"] for t, bar in today.items()}):
            log.info("%s %s %s x%d at %s. %s", o.status, o.side, o.ticker, o.qty, o.fill_price or "-", o.note)
        for tr in broker.check_stops(d, {t: b["Open"] for t, b in today.items()},
                                     {t: b["Low"] for t, b in today.items()}):
            log.info("STOP-LOSS %s sold x%d at %.2f (net P&L Rs %s).", tr.ticker, tr.qty, tr.exit_price,
                     f"{tr.net_pnl:,.0f}")

        # 4) mark to market, daily loss limit
        closes = {t: float(h["Close"].iloc[-1]) for t, h in history.items() if len(h)}
        broker.mark(closes)
        equity = broker.equity()
        halted, change, halt_reason = self.risk.daily_loss(prev_equity, equity)
        if halted:
            log.warning(halt_reason)

        # 5) decisions
        decisions = self._decide(d, history, today, broker, selected, equity, halted, halt_reason)
        for row in decisions:
            log.info("[%s] %-14s %-14s %-15s %s", d, row["ticker"], row["strategy"], row["action"], row["reason"])

        # 6) persist
        trades_today = [t for t in broker.trades if t.exit_date == d]
        bench_close = None
        if bench is not None and len(bench.loc[:day]):
            bench_close = float(bench.loc[:day, "Close"].iloc[-1])
        state = {"broker": broker.to_dict(), "selection": selection_state, "equity": equity}
        ops += [
            upsert("decisions", decisions),
            upsert("orders", [self._order_row(o) for o in broker.drain_touched()]),
            upsert("trades", [vars(t) for t in trades_today]),
            replace("positions", [self._position_row(p, d) for p in broker.positions.values()]),
            upsert("daily_snapshots", [{
                "as_of_date": d, "cash": broker.cash, "positions_value": broker.positions_value(),
                "equity": equity, "benchmark_close": bench_close, "daily_pnl_pct": change,
                "open_positions": len(broker.positions), "max_open_positions": cfg.risk.max_open_positions,
                "max_daily_loss_pct": cfg.risk.max_daily_loss_pct, "trading_halted": halted,
                "halt_reason": halt_reason, "selected_strategies": selected,
                "selection_note": selection_state["summary"], "state_json": state,
                "updated_at": dt.datetime.now(dt.timezone.utc),
            }]),
        ]
        return ops, selection_state, equity, decisions

    def _rerank_due(self, selection_state: dict | None, d: dt.date) -> bool:
        if not selection_state:
            return True
        last = dt.date.fromisoformat(selection_state["as_of"])
        return (d - last).days >= self.cfg.selection.rerank_every_days

    def _decide(self, d: dt.date, history, today, broker: PaperBroker, selected: list[str], equity: float,
                halted: bool, halt_reason: str) -> list[dict[str, Any]]:
        rank = {name: i for i, name in enumerate(selected)}
        ordered = sorted(self.strategies, key=lambda s: rank.get(s.name, len(rank)))
        pending_buys = sum(1 for o in broker.pending_orders if o.side == BUY)
        rows: list[dict[str, Any]] = []
        now = dt.datetime.now(dt.timezone.utc)

        def add(ticker, strategy, signal, action, reason, close):
            rows.append({"id": f"{d}|{ticker}|{strategy}", "as_of_date": d, "ticker": ticker,
                         "strategy": strategy, "signal": signal, "action": action, "reason": reason,
                         "close_price": close, "created_at": now})

        for ticker in self.cfg.universe:
            if ticker not in today:
                add(ticker, "-", Signal.HOLD.value, A_NO_DATA, "No price data for this stock today; skipped.", None)
                continue
            close = float(today[ticker]["Close"])
            pos = broker.positions.get(ticker)
            queued = broker.pending_for(ticker)  # orders left over (no open price yet), checked before new ones
            buy_handled_by: str | None = None

            for strat in ordered:
                dec = strat.decide(history[ticker])
                sig, why = dec.signal, dec.reason

                if queued:
                    add(ticker, strat.name, sig.value, A_HOLD,
                        f"A {queued[0].side} order is already queued for this stock. {why}", close)
                elif pos is not None:
                    owner = self.by_name.get(pos.strategy)
                    if strat.name == pos.strategy:
                        if sig == Signal.SELL:
                            broker.place_order(d, ticker, SELL, pos.qty, strat.name, why)
                            add(ticker, strat.name, sig.value, A_SELL,
                                f"{why} Selling all {pos.qty} shares at the next open.", close)
                        else:
                            add(ticker, strat.name, sig.value, A_HOLD,
                                f"Holding {pos.qty} shares bought at Rs {pos.entry_price:,.2f} "
                                f"(stop-loss Rs {pos.stop_price:,.2f}). {why}", close)
                    else:
                        manager = owner.label if owner else f"{pos.strategy} (now disabled; only the stop-loss applies)"
                        add(ticker, strat.name, sig.value, A_HOLD,
                            f"Already holding this stock, managed by {manager}. This strategy's view: {why}", close)
                elif strat.name not in rank:
                    if sig == Signal.BUY:
                        add(ticker, strat.name, sig.value, A_NOT_SELECTED,
                            f"{why} Not acting: this strategy did not beat buy-and-hold out-of-sample, "
                            "so it is not allowed to trade.", close)
                    else:
                        add(ticker, strat.name, sig.value, A_HOLD, f"{why} (Strategy not selected for trading.)", close)
                elif sig == Signal.BUY:
                    if buy_handled_by:
                        add(ticker, strat.name, sig.value, A_HOLD,
                            f"{why} Not acting: the higher-ranked {buy_handled_by} already handled this stock.", close)
                        continue
                    buy_handled_by = strat.label
                    rd = self.risk.check_new_position(
                        price=close, equity=equity, cash_available=broker.cash - broker.reserved_cash(),
                        open_positions=len(broker.positions), pending_buys=pending_buys,
                        halted=halted, halt_reason=halt_reason)
                    if not rd.allowed:
                        add(ticker, strat.name, sig.value, A_BLOCKED, f"{why} Blocked by risk rules: {rd.reason}", close)
                        continue
                    try:
                        broker.place_order(d, ticker, BUY, rd.qty, strat.name, why, max_notional=rd.max_notional)
                    except BrokerError as exc:
                        add(ticker, strat.name, sig.value, A_BLOCKED, f"{why} Order rejected: {exc}.", close)
                        continue
                    pending_buys += 1
                    add(ticker, strat.name, sig.value, A_BUY, f"{why} {rd.reason} Order fills at the next open.", close)
                elif sig == Signal.SELL:
                    add(ticker, strat.name, sig.value, A_HOLD, f"{why} Not holding this stock, so nothing to sell.", close)
                else:
                    add(ticker, strat.name, sig.value, A_HOLD, why, close)
        return rows

    @staticmethod
    def _order_row(o) -> dict[str, Any]:
        return {k: getattr(o, k) for k in ("order_id", "created_date", "ticker", "side", "qty", "strategy", "reason",
                                           "status", "fill_date", "fill_price", "fees", "note")}

    @staticmethod
    def _position_row(p, d: dt.date) -> dict[str, Any]:
        cost = p.qty * p.entry_price + p.entry_fees
        return {"ticker": p.ticker, "qty": p.qty, "entry_date": p.entry_date, "entry_price": p.entry_price,
                "stop_price": p.stop_price, "strategy": p.strategy, "last_price": p.last_price,
                "market_value": p.market_value, "unrealized_pnl": p.unrealized_pnl,
                "unrealized_pnl_pct": p.unrealized_pnl / cost if cost else 0.0, "as_of_date": d}
