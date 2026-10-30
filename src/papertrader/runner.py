"""Daily pipeline. One call processes every completed trading day not yet processed.

For each new trading day D (in order):
  1. Re-rank strategies if due (backtests use data up to D only).
  2. Fill orders queued at the previous close at D's open.
  3. Check stop-losses / take-profit targets against D's low / high.
  4. Mark positions at D's close (trailing stops move up); check the daily loss limit.
  5. Work out what blocks new buys: daily loss halt, kill switch, protections
     (stop-loss guard, drawdown, cooldown, low-profit lock), market regime and
     data sanity. Exits are never blocked.
  6. Ask every strategy about every stock at D's close. For a BUY that nothing
     above blocked, the trading agent's desks (results calendar, news, insider
     deals, the signal's past odds) write a brief; a BLOCK verdict skips the buy.
     Then risk rules size it and an order is queued for the next open. Every
     decision is logged with its reason.
  7. Save decisions, orders, trades, positions, agent briefs and a snapshot.
  8. After the last day: the agent's chart scan of the wider universe, and the
     rotation strategy's separate paper account (portfolio/paper.py), if enabled.

The agent desks read live news and filings, so they only run for the most
recent day; catch-up days (missed runs) trade on the strategy rules alone.

Re-running is safe: state is loaded from the latest saved snapshot, so a day that
was already processed is skipped (or, with force=True, recomputed from the
snapshot before it and overwritten by primary key, never duplicated).
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import platform
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from papertrader import protections
from papertrader.backtest import market_regime
from papertrader.broker import BUY, SELL, BrokerError, PaperBroker
from papertrader.costs import split_ratio
from papertrader.config import Config
from papertrader.data import DataError, complete_calendar, load_prices
from papertrader.indicators import atr
from papertrader.notify import format_summary, send_telegram
from papertrader.risk import RiskManager
from papertrader.selection import Selection, evaluate_strategies
from papertrader.agent import market
from papertrader.storage import StateUnavailableError, Store, WriteOp, delete_eq, replace, upsert
from papertrader.strategies import Signal, Strategy, build_strategies

log = logging.getLogger("papertrader.runner")

# Actions recorded for each (stock, strategy) decision.
A_BUY, A_SELL, A_HOLD = "BUY", "SELL", "HOLD"
A_BLOCKED, A_NOT_SELECTED, A_NO_DATA = "BLOCKED_BY_RISK", "NOT_SELECTED", "NO_DATA"

PriceLoader = Callable[..., tuple[dict[str, pd.DataFrame], list[str]]]
Selector = Callable[..., Selection]
Notifier = Callable[[str], bool]


@dataclass
class RunSummary:
    run_id: str
    status: str  # ok | noop | aborted | failed
    message: str
    as_of_date: dt.date | None = None
    days_processed: list[dt.date] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)  # fills, stops, halts: for notifications
    briefs: list[str] = field(default_factory=list)  # agent briefs, one line each
    scan: list[str] = field(default_factory=list)    # top chart-scan setups


class DailyRunner:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        strategies: list[Strategy] | None = None,
        price_loader: PriceLoader = load_prices,
        selector: Selector = evaluate_strategies,
        now: dt.datetime | None = None,
        notifier: Notifier | None = None,
        coordinator: Any = None,
        scanner: Callable[..., list[dict[str, Any]]] | None = None,
        rotation: Callable[[], list[str]] | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.strategies = strategies if strategies is not None else build_strategies(cfg.strategies)
        self.by_name = {s.name: s for s in self.strategies}
        self.price_loader = price_loader
        self.selector = selector
        self.tz = ZoneInfo(cfg.data.market_tz)
        self.now = now.astimezone(self.tz) if now else dt.datetime.now(self.tz)
        self.costs = cfg.costs.model()
        self.exits = cfg.risk.exit_rules()
        self.risk = RiskManager(cfg.risk, self.costs)
        self.notifier = notifier if notifier is not None else (send_telegram if cfg.notify.telegram else None)
        if coordinator is None and cfg.agent.enabled:
            from papertrader.agent.orchestrator import Coordinator

            from papertrader.agent.orchestrator import make_brain

            try:
                brain = make_brain(cfg.sections.get("brain"))
            except Exception as exc:  # a bad brain section must not stop trading
                log.warning("Brain not started: %s", exc)
                brain = None
            coordinator = Coordinator(cfg.agent, self.costs, self.exits, brain=brain)
        self.coordinator = coordinator
        self.scanner = scanner
        self.rotation = rotation
        self._agent_live = False
        self._briefs: list[Any] = []

    def kill_switch(self) -> str:
        """Reason new buys are switched off by the operator, or ''."""
        if os.environ.get("PAPERTRADER_KILL_SWITCH", "").strip().lower() in ("1", "true", "yes", "on"):
            return "Kill switch is ON (PAPERTRADER_KILL_SWITCH): no new buys; exits and stops still run."
        if self.cfg.trading.kill_switch_file.exists():
            return (f"Kill switch is ON (file {self.cfg.trading.kill_switch_file.name} exists): "
                    "no new buys; exits and stops still run.")
        return ""

    # ------------------------------------------------------------------ run
    def run(self, force: bool = False, refresh: bool = False) -> RunSummary:
        run_id = f"{self.now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        started = dt.datetime.now(dt.timezone.utc)
        summary = RunSummary(run_id, "failed", "")
        log.info("=== PAPER TRADING run %s (no real money) ===", run_id)
        try:
            busy = self.store.other_run_active(run_id)
            if busy:
                summary.status, summary.message = "noop", f"Another run is in progress ({busy}); not starting a second."
                log.warning(summary.message)
                return summary
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
        if self.notifier and summary.status != "noop":
            try:
                self.notifier(format_summary(summary))
            except Exception:
                log.exception("Notification failed")
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

        calendar = complete_calendar(universe)
        last_bar = calendar[-1].date()
        saved = self.store.load_latest_state(before=last_bar if force else None)

        if saved is None:
            days = [calendar[-1]]
            broker = PaperBroker(cfg.capital, self.costs, cfg.risk.stop_loss_pct, self.exits)
            selection_state, prev_equity = None, cfg.capital
            history_state: dict[str, list] = {"recent_trades": [], "equity_history": []}
            log.info("No saved state: starting a fresh paper account with Rs %s.", f"{cfg.capital:,.0f}")
        else:
            saved_date, state = saved
            days = [d for d in calendar if saved_date < d.date() <= last_bar]
            broker = PaperBroker.from_dict(state["broker"], self.costs, cfg.risk.stop_loss_pct, self.exits)
            selection_state, prev_equity = state.get("selection"), float(state["equity"])
            history_state = {"recent_trades": list(state.get("recent_trades") or []),
                             "equity_history": list(state.get("equity_history") or [])}
            log.info("Loaded state as of %s (equity Rs %s).", saved_date, f"{prev_equity:,.0f}")

        summary.as_of_date = last_bar
        if not market.holidays_known(last_bar.year):
            warn = (f"NSE holidays for {last_bar.year} are not in agent/market.py yet: month-end and "
                    "results-blackout dates may be off. Add them from NSE's holiday circular.")
            log.warning(warn)
            summary.events.append(warn)
        if not days:
            summary.status = "noop"
            summary.message = f"Already processed the latest completed trading day ({last_bar}); nothing to do."
            return

        for day in days:
            # Agent desks read today's news: only for the latest day, and only before the next session
            # opens (a run delayed past the next open would judge yesterday's close with later news).
            next_open = dt.datetime.combine(market.next_trading_day(day.date()), market.OPEN, market.IST)
            self._agent_live = self.coordinator is not None and day == days[-1] and self.now < next_open
            if self.coordinator is not None and day == days[-1] and not self._agent_live:
                log.warning("Run is past the next session's open: agent desks skipped for %s.", day.date())
            ops, selection_state, prev_equity, decisions = self._process_day(
                day, universe, bench, broker, selection_state, prev_equity, history_state, summary.events)
            if force:  # a recompute replaces the day: clear rows the first pass made but this one may not
                d = day.date()
                ops = [delete_eq("decisions", "as_of_date", d), delete_eq("orders", "created_date", d),
                       delete_eq("agent_briefs", "as_of_date", d)] + ops
            self.store.write(ops)
            summary.days_processed.append(day.date())
            summary.decisions = decisions

        summary.briefs = [f"{b.ticker} {b.strategy}: {b.summary()}" for b in self._briefs]
        if self.cfg.agent.enabled and self.cfg.agent.scan_universe != "none":
            summary.scan = self._scan(last_bar, universe)
        summary.events += self._rotation()

        n_trades = sum(1 for d in summary.decisions if d["action"] in (A_BUY, A_SELL))
        summary.status = "ok"
        summary.message = (f"Processed {len(days)} trading day(s) up to {last_bar}. "
                           f"Equity Rs {prev_equity:,.0f}. {n_trades} order(s) queued for the next open. "
                           + (f"Data warnings: {'; '.join(warnings)}" if warnings else ""))

    # ------------------------------------------------------------- one day
    def _process_day(self, day: pd.Timestamp, universe: dict[str, pd.DataFrame], bench: pd.DataFrame | None,
                     broker: PaperBroker, selection_state: dict | None, prev_equity: float,
                     history_state: dict[str, list], events: list[str]):
        cfg, d = self.cfg, day.date()
        log.info("--- Processing %s ---", d)
        history = {t: df.loc[:day] for t, df in universe.items()}
        today = {t: df.loc[day] for t, df in universe.items() if day in df.index}
        bench_hist = bench.loc[:day] if bench is not None else None
        ops: list[WriteOp] = []

        # 1) strategy selection
        if self._rerank_due(selection_state, d):
            selection = self.selector(self.strategies, history, cfg, d, benchmark=bench_hist)
            selection_state = selection.to_state()
            ops.append(upsert("backtest_results", selection.to_rows()))
            log.info("Strategy selection re-ranked: %s", selection.summary())
        selected: list[str] = selection_state["selected"]

        # 1b) splits / bonuses: rescale held positions before any stop is checked
        for t, pos in list(broker.positions.items()):
            h = history.get(t)
            if h is not None and len(h) >= 2:
                ratio = split_ratio(pos.last_price, float(h["Close"].iloc[-2]))
                if ratio:
                    note = broker.apply_split(t, ratio)
                    log.warning(note)
                    events.append(f"{d} {note}")

        # 2) fills at today's open, 3) stops
        for o in broker.fill_pending(d, {t: bar["Open"] for t, bar in today.items()}):
            log.info("%s %s %s x%d at %s. %s", o.status, o.side, o.ticker, o.qty, o.fill_price or "-", o.note)
            events.append(f"{d} {o.status} {o.side} {o.ticker} x{o.qty} at {o.fill_price or '-'} {o.note}".strip())
        for tr in broker.check_exits(d, {t: b["Open"] for t, b in today.items()},
                                     {t: b["High"] for t, b in today.items()},
                                     {t: b["Low"] for t, b in today.items()}):
            label = "STOP-LOSS" if tr.exit_reason == "stop_loss" else "TAKE-PROFIT"
            log.info("%s %s sold x%d at %.2f (net P&L Rs %s).", label, tr.ticker, tr.qty, tr.exit_price,
                     f"{tr.net_pnl:,.0f}")
            events.append(f"{d} {label} {tr.ticker} x{tr.qty} at {tr.exit_price:,.2f} (net Rs {tr.net_pnl:,.0f})")

        # 4) mark to market (trails stops), daily loss limit
        closes = {t: float(h["Close"].iloc[-1]) for t, h in history.items() if len(h)}
        broker.mark(closes)
        equity = broker.equity()
        halted, change, halt_reason = self.risk.daily_loss(prev_equity, equity)
        if halted:
            log.warning(halt_reason)
            events.append(f"{d} {halt_reason}")

        # 5) what blocks new buys today
        trades_today = [t for t in broker.trades if t.exit_date == d]
        recent = history_state["recent_trades"] + [
            {"ticker": t.ticker, "exit_date": t.exit_date.isoformat(), "exit_reason": t.exit_reason,
             "return_pct": t.return_pct} for t in trades_today]
        eq_hist = [e for e in history_state["equity_history"] if str(e[0]) != d.isoformat()] + [[d.isoformat(), equity]]
        history_state["recent_trades"], history_state["equity_history"] = protections.prune(d, recent, eq_hist)
        prot = protections.evaluate(d, cfg.protections, history_state["recent_trades"],
                                    history_state["equity_history"])
        buy_block = halt_reason if halted else (self.kill_switch()
                                                or (prot.global_lock.reason if prot.global_lock else "")
                                                or self._regime_block(bench_hist))
        if buy_block and not halted:
            log.warning(buy_block)
        ticker_block = {t: lk.reason for t, lk in prot.ticker_locks.items()}
        ticker_block.update(self._data_sanity(history))

        # 6) decisions
        decisions = self._decide(d, history, today, broker, selected, equity, buy_block, ticker_block)
        for row in decisions:
            log.info("[%s] %-14s %-14s %-15s %s", d, row["ticker"], row["strategy"], row["action"], row["reason"])

        # 7) persist
        bench_close = None
        if bench_hist is not None and len(bench_hist):
            bench_close = float(bench_hist["Close"].iloc[-1])
        state = {"broker": broker.to_dict(), "selection": selection_state, "equity": equity, **history_state}
        day_briefs = [b for b in self._briefs if b.as_of_date == d]
        ops += [
            upsert("decisions", decisions),
            upsert("agent_briefs", [b.to_row() for b in day_briefs]),
            upsert("orders", [self._order_row(o) for o in broker.drain_touched()]),
            upsert("trades", [vars(t) for t in trades_today]),
            replace("positions", [self._position_row(p, d) for p in broker.positions.values()]),
            upsert("daily_snapshots", [{
                "as_of_date": d, "cash": broker.cash, "positions_value": broker.positions_value(),
                "equity": equity, "benchmark_close": bench_close, "daily_pnl_pct": change,
                "open_positions": len(broker.positions), "max_open_positions": cfg.risk.max_open_positions,
                "max_daily_loss_pct": cfg.risk.max_daily_loss_pct, "trading_halted": bool(buy_block),
                "halt_reason": buy_block, "selected_strategies": selected,
                "selection_note": selection_state["summary"], "state_json": state,
                "updated_at": dt.datetime.now(dt.timezone.utc),
            }]),
        ]
        return ops, selection_state, equity, decisions

    def _rotation(self) -> list[str]:
        """Run the rotation paper account (its own cash and holdings). Never fails the run."""
        if self.rotation is None and not (self.cfg.sections.get("portfolio") or {}).get("enabled"):
            return []
        try:
            if self.rotation is not None:
                return self.rotation()
            from papertrader.portfolio.paper import RotationPaper
            return RotationPaper(self.cfg, self.store, now=self.now).run()
        except Exception as exc:
            log.warning("Rotation paper account skipped: %s", exc)
            return [f"Rotation paper account skipped: {exc}"[:300]]

    def _scan(self, d: dt.date, universe: dict[str, pd.DataFrame]) -> list[str]:
        """Agent chart scan of the wider universe on the latest day. Never fails the run."""
        try:
            scanner = self.scanner
            if scanner is None:
                from papertrader.agent.scan_job import run_scan as scanner
            hits = scanner(self.cfg, d, universe, now=self.now)
        except Exception as exc:
            log.warning("Agent chart scan skipped: %s", exc)
            return []
        top = hits[: self.cfg.agent.scan_top_n]
        now = dt.datetime.now(dt.timezone.utc)
        try:
            self.store.write([upsert("agent_scans", [{
                "id": f"{d}|{h['ticker']}|{h['setup']}", "as_of_date": d, "ticker": h["ticker"],
                "setup": h["setup"], "close_price": h.get("close"), "score": h.get("score"),
                "detail": str(h.get("detail", ""))[:1000], "created_at": now} for h in top])])
        except Exception as exc:
            log.warning("Could not save the chart scan: %s", exc)
        log.info("Agent chart scan: %d setups on %s.", len(hits), d)
        return [f"{h['ticker']} {h['setup']}: {h.get('detail', '')}"[:200] for h in top[:8]]

    def _rerank_due(self, selection_state: dict | None, d: dt.date) -> bool:
        if not selection_state:
            return True
        last = dt.date.fromisoformat(selection_state["as_of"])
        return (d - last).days >= self.cfg.selection.rerank_every_days

    def _regime_block(self, bench_hist: pd.DataFrame | None) -> str:
        rg = self.cfg.regime
        if not rg.enabled:
            return ""
        series = market_regime(bench_hist, rg.sma_window)
        if series is None or series.empty:
            return f"Market regime unknown (no {self.cfg.benchmark} data): no new buys to be safe."
        if not bool(series.iloc[-1]):
            return (f"Market regime filter: {self.cfg.benchmark} closed below its {rg.sma_window}-day average, "
                    "so no new buys (exits still run).")
        return ""

    def _data_sanity(self, history: dict[str, pd.DataFrame]) -> dict[str, str]:
        """Stocks whose latest close moved implausibly far: probably a bad or unadjusted bar."""
        limit = self.cfg.risk.max_price_jump_pct / 100.0
        out = {}
        for t, h in history.items():
            if len(h) >= 2:
                prev, last = float(h["Close"].iloc[-2]), float(h["Close"].iloc[-1])
                if prev > 0 and abs(last / prev - 1.0) > limit:
                    out[t] = (f"Data check: close moved {last / prev - 1.0:+.1%} in one day "
                              f"(limit {limit:.0%}); possible data error, so no new buy.")
        return out

    def _atr(self, hist: pd.DataFrame) -> float | None:
        """ATR at the signal close, needed only for ATR stops (fixed stops use a fixed distance)."""
        if self.cfg.risk.stop_mode != "atr" or not len(hist):
            return None
        val = atr(hist, self.cfg.risk.atr_period).iloc[-1]
        return None if pd.isna(val) else float(val)

    def _decide(self, d: dt.date, history, today, broker: PaperBroker, selected: list[str], equity: float,
                buy_block: str, ticker_block: dict[str, str]) -> list[dict[str, Any]]:
        rank = {name: i for i, name in enumerate(selected)}
        ordered = sorted(self.strategies, key=lambda s: rank.get(s.name, len(rank)))
        pending_buys = sum(1 for o in broker.pending_orders if o.side == BUY)
        orders_today = 0
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
                        note = ""
                        if self._agent_live:
                            b = self.coordinator.brief(d, ticker, strat, history[ticker], info_only=True, reason=why)
                            self._briefs.append(b)
                            note = f" {b.summary()}"
                        add(ticker, strat.name, sig.value, A_NOT_SELECTED,
                            f"{why} Not acting: this strategy did not beat buy-and-hold out-of-sample, "
                            f"so it is not allowed to trade.{note}", close)
                    else:
                        add(ticker, strat.name, sig.value, A_HOLD, f"{why} (Strategy not selected for trading.)", close)
                elif sig == Signal.BUY:
                    if buy_handled_by:
                        add(ticker, strat.name, sig.value, A_HOLD,
                            f"{why} Not acting: the higher-ranked {buy_handled_by} already handled this stock.", close)
                        continue
                    buy_handled_by = strat.label
                    blocked = buy_block or ticker_block.get(ticker)
                    if blocked:
                        add(ticker, strat.name, sig.value, A_BLOCKED, f"{why} Blocked: {blocked}", close)
                        continue
                    atr_now = self._atr(history[ticker])
                    rd = self.risk.check_new_position(
                        price=close, equity=equity, cash_available=broker.cash - broker.reserved_cash(),
                        open_positions=len(broker.positions), pending_buys=pending_buys,
                        halted=False, atr_at_signal=atr_now, orders_today=orders_today)
                    if not rd.allowed:
                        add(ticker, strat.name, sig.value, A_BLOCKED, f"{why} Blocked by risk rules: {rd.reason}", close)
                        continue
                    # The agent is asked only about buys that would really be placed (saves LLM calls).
                    agent_note = ""
                    if self._agent_live:
                        b = self.coordinator.brief(d, ticker, strat, history[ticker], reason=why)
                        self._briefs.append(b)
                        if b.blocked:
                            add(ticker, strat.name, sig.value, A_BLOCKED, f"{why} Blocked by the agent: {b.reason()}",
                                close)
                            continue
                        agent_note = f" {b.summary()}"
                    try:
                        broker.place_order(d, ticker, BUY, rd.qty, strat.name, why, max_notional=rd.max_notional,
                                           atr_at_signal=atr_now)
                    except BrokerError as exc:
                        add(ticker, strat.name, sig.value, A_BLOCKED, f"{why} Order rejected: {exc}.", close)
                        continue
                    pending_buys += 1
                    orders_today += 1
                    add(ticker, strat.name, sig.value, A_BUY,
                        f"{why} {rd.reason} Order fills at the next open.{agent_note}", close)
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
