"""OrderExecutor: turns the bot's decided orders into broker orders, safely.

Modes
  paper   records what the bot decided; the PaperBroker does the (simulated) filling.
          Nothing goes to any broker.
  shadow  builds the exact OrderRequests it WOULD send (NSE symbol, tick-rounded
          protected limit price, tag, client_order_id), writes them to the audit log,
          and reconciles broker holdings against the bot's positions. It never calls
          place_order.
  live    refuses with LiveTradingLocked unless EVERY gate below holds. Today the
          first gate cannot hold (config.py accepts only trading.mode: paper) and no
          verified broker adapter exists, so live is unreachable.

Live gates (all re-checked on every execute call):
  1. config trading.mode is "live" (passed in as `config_mode`)
  2. env PAPERTRADER_LIVE_CONFIRM == "I-ACCEPT-REAL-MONEY-RISK"
  3. a positive capital cap (default Rs 25,000), and money already at the broker plus
     today's planned buys stays within it
  4. the kill switch is off (env PAPERTRADER_KILL_SWITCH and the KILL_SWITCH file)
  5. data/live/preflight.json exists, is under 24 hours old and says passed
  6. a broker session is attached, its class says `verified = True`, it is not the
     SimulatedExchange, and it is logged in

Every order, in every mode, also passes: max orders per day, max notional per order,
and a deterministic client_order_id (hash of date|ticker|side|strategy) so a retried run
sees "already handled" instead of sending twice. Every intent and decision is appended
to an audit log (JSONL).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from papertrader.agent.market import now_ist
from papertrader.config import PROJECT_ROOT
from papertrader.live.interface import (BUY, CNC, LIMIT, MARKET, SELL, BrokerOrder, BrokerSession, InvalidOrder,
                                        OrderRequest, nse_symbol, round_to_tick)
from papertrader.live.reconcile import ReconcileReport, reconcile_session
from papertrader.live.simulated import SimulatedExchange

log = logging.getLogger(__name__)

MODES = ("paper", "shadow", "live")
LIVE_CONFIRM_ENV = "PAPERTRADER_LIVE_CONFIRM"
LIVE_CONFIRM_PHRASE = "I-ACCEPT-REAL-MONEY-RISK"
KILL_SWITCH_ENV = "PAPERTRADER_KILL_SWITCH"
DEFAULT_CAPITAL_CAP = 25_000.0
PREFLIGHT_MAX_AGE = dt.timedelta(hours=24)
LIVE_DIR = PROJECT_ROOT / "data" / "live"
DEFAULT_AUDIT_PATH = LIVE_DIR / "audit.jsonl"
DEFAULT_PREFLIGHT_PATH = LIVE_DIR / "preflight.json"
DEFAULT_RECONCILE_PATH = LIVE_DIR / "reconcile_latest.json"
DEFAULT_KILL_SWITCH_FILE = PROJECT_ROOT / "KILL_SWITCH"

# audit events
PAPER, WOULD_SEND, SENT, SKIPPED, DUPLICATE = "paper", "would_send", "sent", "skipped", "duplicate"
COUNTED_EVENTS = (PAPER, WOULD_SEND, SENT)


class LiveTradingLocked(RuntimeError):
    """Live order placement is locked. The message lists every unmet gate."""


def make_client_order_id(day: dt.date | str, ticker: str, side: str, strategy: str) -> str:
    """Deterministic 20-character id: the same (date, ticker, side, strategy) always gives
    the same id, so a retry can never create a second order. Fits the 20-character tag."""
    key = f"{str(day)[:10]}|{nse_symbol(ticker)}|{side.upper()}|{strategy}"
    return "PT" + hashlib.sha256(key.encode()).hexdigest()[:18].upper()


def kill_switch_on(env: Mapping[str, str] | None = None, file: Path | str | None = None) -> bool:
    """Same rule as the runner: env PAPERTRADER_KILL_SWITCH in (1, true, yes) or the file exists."""
    env = os.environ if env is None else env
    if str(env.get(KILL_SWITCH_ENV, "")).strip().lower() in ("1", "true", "yes", "on"):
        return True
    return Path(file or DEFAULT_KILL_SWITCH_FILE).exists()


def load_preflight(path: Path | str | None = None) -> dict[str, Any] | None:
    p = Path(path or DEFAULT_PREFLIGHT_PATH)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def preflight_problem(path: Path | str | None = None, now: dt.datetime | None = None,
                      max_age: dt.timedelta = PREFLIGHT_MAX_AGE) -> str:
    """'' when the preflight report is fresh and passing, else the reason in plain words."""
    report = load_preflight(path)
    if report is None:
        return "no preflight report (run scripts/preflight.py)"
    try:
        stamp = now_ist(dt.datetime.fromisoformat(str(report["timestamp"])))
    except (KeyError, ValueError):
        return "preflight report has no valid timestamp"
    age = now_ist(now) - stamp
    if age > max_age or age < -dt.timedelta(minutes=5):
        return f"preflight report is {age.total_seconds() / 3600:.1f} h old (must be under {max_age.total_seconds() / 3600:.0f} h)"
    if report.get("passed") is not True:
        failed = [r.get("name", "?") for r in report.get("results", []) if not r.get("ok")]
        return "preflight did not pass" + (f" (failed: {', '.join(failed)})" if failed else "")
    return ""


@dataclass(frozen=True)
class ExecutorLimits:
    capital_cap: float | None = DEFAULT_CAPITAL_CAP   # INR; live refuses without a positive cap
    max_orders_per_day: int = 10
    max_notional_per_order: float = 10_000.0          # INR
    price_protection_pct: float = 0.5                 # limit = ref price +/- this, rounded to tick
    product: str = CNC


@dataclass(frozen=True)
class OrderIntent:
    """An order the bot has decided on, before it becomes a broker request."""
    day: dt.date
    ticker: str
    side: str
    qty: int
    strategy: str
    ref_price: float                  # latest price the decision was based on
    reason: str = ""
    order_type: str = LIMIT           # LIMIT (protected) or MARKET
    product: str | None = None        # None = the executor's default product

    @property
    def client_order_id(self) -> str:
        return make_client_order_id(self.day, self.ticker, self.side, self.strategy)

    @classmethod
    def from_order(cls, order: Any, ref_price: float, day: dt.date | None = None) -> "OrderIntent":
        """From a papertrader.broker.Order (the daily runner's queued order)."""
        return cls(day=day or order.created_date, ticker=order.ticker, side=order.side, qty=int(order.qty),
                   strategy=order.strategy, ref_price=float(ref_price), reason=order.reason)


@dataclass
class ExecutionResult:
    intent: OrderIntent
    action: str                                  # paper | would_send | sent | skipped | duplicate
    request: OrderRequest | None = None
    reason: str = ""
    broker_order: BrokerOrder | None = None


@dataclass
class ExecutionReport:
    mode: str
    day: dt.date
    results: list[ExecutionResult] = field(default_factory=list)
    reconciliation: ReconcileReport | None = None

    def by_action(self, action: str) -> list[ExecutionResult]:
        return [r for r in self.results if r.action == action]

    def summary(self) -> str:
        counts = {a: len(self.by_action(a)) for a in (PAPER, WOULD_SEND, SENT, SKIPPED, DUPLICATE)}
        text = f"Executor ({self.mode}) {self.day}: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v)
        if self.reconciliation is not None:
            text += "\n" + self.reconciliation.summary()
        return text


class OrderExecutor:
    def __init__(self, mode: str = "paper", session: BrokerSession | None = None, *,
                 limits: ExecutorLimits | None = None, config_mode: str = "paper",
                 audit_path: Path | str | None = None, preflight_path: Path | str | None = None,
                 reconcile_path: Path | str | None = None, kill_switch_file: Path | str | None = None,
                 env: Mapping[str, str] | None = None, clock: Callable[[], dt.datetime] | None = None) -> None:
        if mode not in MODES:
            raise ValueError(f"mode {mode!r} must be one of {MODES}")
        self.mode = mode
        self.session = session
        self.limits = limits or ExecutorLimits()
        self.config_mode = config_mode
        self.audit_path = Path(audit_path or DEFAULT_AUDIT_PATH)
        self.preflight_path = Path(preflight_path or DEFAULT_PREFLIGHT_PATH)
        self.reconcile_path = Path(reconcile_path or DEFAULT_RECONCILE_PATH)
        self.kill_switch_file = Path(kill_switch_file or DEFAULT_KILL_SWITCH_FILE)
        self.env = env
        self.clock = clock
        if mode == "live":
            self.assert_live_unlocked()

    # ----- time / env -----
    def now(self) -> dt.datetime:
        return now_ist(self.clock() if self.clock else None)

    def _env(self) -> Mapping[str, str]:
        return os.environ if self.env is None else self.env

    # ----- request building -----
    def build_request(self, intent: OrderIntent) -> OrderRequest:
        """The exact broker request for an intent: NSE symbol, tick-rounded protected limit
        (buys rounded down, sells up), tag = client_order_id. Raises InvalidOrder."""
        if intent.side not in (BUY, SELL):
            raise InvalidOrder(f"side {intent.side!r} must be BUY or SELL")
        if not intent.ref_price or intent.ref_price <= 0:
            raise InvalidOrder(f"{intent.ticker}: no reference price")
        cid = intent.client_order_id
        if intent.order_type == MARKET:
            price = None
        else:
            p = self.limits.price_protection_pct / 100.0
            raw = intent.ref_price * (1 + p) if intent.side == BUY else intent.ref_price * (1 - p)
            price = round_to_tick(raw, side=intent.side)
        req = OrderRequest(symbol=intent.ticker, side=intent.side, qty=int(intent.qty),
                           product=intent.product or self.limits.product, order_type=intent.order_type,
                           price=price, tag=cid, client_order_id=cid)
        return req.validate()

    # ----- the live lock -----
    def _deployed_at_broker(self) -> float:
        s = self.session
        if s is None:
            return 0.0
        value = sum(h.total_qty * (h.last_price or h.avg_price) for h in s.holdings())
        value += sum(max(p.qty, 0) * (p.last_price or p.avg_price) for p in s.positions())
        return value

    def live_lock_reasons(self, planned_buy_notional: float = 0.0) -> list[str]:
        """Every unmet live gate, in plain words. Empty list = unlocked."""
        reasons: list[str] = []
        if self.config_mode != "live":
            reasons.append(f"config trading.mode is {self.config_mode!r}, not 'live' "
                           "(config.py accepts only 'paper' today)")
        if self._env().get(LIVE_CONFIRM_ENV) != LIVE_CONFIRM_PHRASE:
            reasons.append(f"env {LIVE_CONFIRM_ENV} is not set to the confirmation phrase")
        if kill_switch_on(self._env(), self.kill_switch_file):
            reasons.append("kill switch is ON")
        problem = preflight_problem(self.preflight_path, self.now())
        if problem:
            reasons.append(problem)
        s = self.session
        session_ok = False
        if s is None:
            reasons.append("no broker session attached")
        elif isinstance(s, SimulatedExchange):
            reasons.append("the SimulatedExchange can never place live orders")
        elif getattr(type(s), "verified", False) is not True:
            reasons.append(f"broker adapter {type(s).__name__} is not marked verified = True")
        else:
            try:
                status = s.session_status()
                session_ok = status.valid_at(self.now())
                if not session_ok:
                    reasons.append(f"broker session not logged in or expired ({status.note or 'log in again'})")
            except Exception as exc:  # an adapter error must lock, never unlock
                reasons.append(f"broker session check failed: {exc}")
        cap = self.limits.capital_cap
        if cap is None or cap <= 0:
            reasons.append("no positive capital cap set")
        elif session_ok:
            try:
                total = self._deployed_at_broker() + planned_buy_notional
                if total > cap + 1e-6:
                    reasons.append(f"capital cap Rs {cap:,.0f} exceeded: Rs {total:,.0f} at broker + planned buys")
            except Exception as exc:
                reasons.append(f"could not read broker holdings for the capital cap: {exc}")
        elif planned_buy_notional > cap + 1e-6:
            reasons.append(f"capital cap Rs {cap:,.0f} exceeded by planned buys Rs {planned_buy_notional:,.0f}")
        return reasons

    def assert_live_unlocked(self, planned_buy_notional: float = 0.0) -> None:
        reasons = self.live_lock_reasons(planned_buy_notional)
        if reasons:
            self._audit({"event": "live_locked", "reasons": reasons})
            raise LiveTradingLocked("LIVE TRADING LOCKED - no order was sent:\n- " + "\n- ".join(reasons))

    # ----- audit log -----
    def _audit(self, record: dict[str, Any]) -> None:
        rec = {"ts": self.now().isoformat(), "mode": self.mode, **record}
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    def audit_records(self, day: dt.date | None = None) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        out = []
        for line in self.audit_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if day is None or rec.get("day") == day.isoformat():
                out.append(rec)
        return out

    # ----- execution -----
    def execute(self, intents: Iterable[OrderIntent], *, bot_positions: Mapping[str, Any] | None = None,
                bot_cash: float | None = None) -> ExecutionReport:
        intents = list(intents)
        day = self.now().date()
        report = ExecutionReport(self.mode, day)

        built: list[tuple[OrderIntent, OrderRequest | None, str]] = []
        for intent in intents:
            try:
                built.append((intent, self.build_request(intent), ""))
            except InvalidOrder as exc:
                built.append((intent, None, f"invalid: {exc}"))

        if self.mode == "live":
            planned = sum(r.notional() for _, r, _ in built if r is not None and r.side == BUY
                          and r.notional() <= self.limits.max_notional_per_order)
            self.assert_live_unlocked(planned)

        todays = [r for r in self.audit_records(day) if r.get("mode") == self.mode]
        handled = {r.get("client_order_id") for r in todays if r.get("event") in COUNTED_EVENTS}
        count = sum(1 for r in todays if r.get("event") in COUNTED_EVENTS)

        for intent, req, problem in built:
            res = ExecutionResult(intent, SKIPPED, req, problem)
            if req is None:
                pass
            elif req.client_order_id in handled:
                res.action, res.reason = DUPLICATE, "already handled today (same client_order_id); not sent again"
            elif req.notional() > self.limits.max_notional_per_order + 1e-6:
                res.reason = (f"notional Rs {req.notional():,.0f} above the per-order limit "
                              f"Rs {self.limits.max_notional_per_order:,.0f}")
            elif count >= self.limits.max_orders_per_day:
                res.reason = f"daily order limit {self.limits.max_orders_per_day} reached"
            elif self.mode == "paper":
                res.action, res.reason = PAPER, "paper mode: the PaperBroker simulates this fill; nothing sent"
            elif self.mode == "shadow":
                res.action, res.reason = WOULD_SEND, "shadow mode: logged only, place_order NOT called"
            else:
                res = self._send_live(intent, req)
            if res.action in COUNTED_EVENTS:
                count += 1
                handled.add(req.client_order_id)
            report.results.append(res)
            self._audit({"event": res.action, "day": day.isoformat(),
                         "client_order_id": req.client_order_id if req else intent.client_order_id,
                         "intent": asdict(intent), "request": asdict(req) if req else None, "reason": res.reason,
                         "broker_order_id": res.broker_order.order_id if res.broker_order else None,
                         "broker_status": res.broker_order.status if res.broker_order else None})

        if self.mode == "shadow" and self.session is not None and bot_positions is not None:
            report.reconciliation = self.shadow_reconcile(bot_positions, bot_cash)
        return report

    def _send_live(self, intent: OrderIntent, req: OrderRequest) -> ExecutionResult:
        """Only reachable after assert_live_unlocked passed. Re-checks the lock per order."""
        self.assert_live_unlocked(req.notional() if req.side == BUY else 0.0)
        order = self.session.place_order(req)  # type: ignore[union-attr]
        return ExecutionResult(intent, SENT, req, order.reject_reason or "sent to broker", order)

    def shadow_reconcile(self, bot_positions: Mapping[str, Any], bot_cash: float | None = None) -> ReconcileReport:
        """Reconcile broker holdings vs the bot's positions; save and audit the report."""
        if self.session is None:
            raise ValueError("shadow reconciliation needs a broker session")
        rep = reconcile_session(self.session, bot_positions, bot_cash=bot_cash, as_of=self.now().date())
        rep.save(self.reconcile_path)
        self._audit({"event": "reconcile", "day": self.now().date().isoformat(), "clean": rep.clean,
                     "worst": rep.worst, "differences": [asdict(d) for d in rep.differences]})
        if not rep.clean:
            log.warning(rep.summary())
        return rep
