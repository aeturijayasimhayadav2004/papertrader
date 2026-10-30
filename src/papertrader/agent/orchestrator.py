"""The coordinator: asks every desk about a buy candidate and writes one brief.

The daily runner calls `Coordinator.brief()` when a strategy says BUY and nothing
else (risk halt, kill switch, protections, regime, data check) has blocked it.

  verdict BLOCK   a desk found a reason not to buy (results this week, fraud
                  headline, the signal's past odds are poor ...); the buy is skipped
  verdict CAUTION a desk raised a warning (promoter selling, downgrade); the buy goes ahead
  verdict GO      nothing against it

A desk whose data cannot be read reports "unknown". That blocks only when
`agent.block_on_unknown` is true, so a flaky free data source never stops the
bot on its own.

The optional brain (Claude, agent/brain.py) reads the desks' findings last and can
only veto: a confident veto is a BLOCK, a weak one a WARN. It is asked only when
it is enabled and has an API key, and its "unknown" (no answer) never blocks. Desks never see future data: the odds desk uses bars up to the
signal day only.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from papertrader.agent.types import BLOCK, UNKNOWN, WARN, Finding, unknown
from papertrader.config import AgentConfig
from papertrader.costs import CostModel
from papertrader.exits import ExitRules
from papertrader.strategies import Strategy

log = logging.getLogger(__name__)

GO, CAUTION = "GO", "CAUTION"
VERDICT_BLOCK = "BLOCK"

# desk(ticker, day, strategy, history) -> Finding
Desk = Callable[[str, dt.date, Strategy, pd.DataFrame], Finding]


@dataclass
class Brief:
    as_of_date: dt.date
    ticker: str
    strategy: str
    verdict: str
    findings: list[Finding] = field(default_factory=list)
    info_only: bool = False      # strategy not selected: the brief is for the record, it gates nothing

    @property
    def blocked(self) -> bool:
        return self.verdict == VERDICT_BLOCK

    def reason(self) -> str:
        """Why the agent blocked (or warned), in one line."""
        keyed = [f for f in self.findings if f.status in (BLOCK, WARN)] or self.findings
        return " ".join(f"[{f.desk}] {f.summary}" for f in keyed)[:900]

    def summary(self) -> str:
        head = {GO: "Agent: no objection.", CAUTION: "Agent: go, with caution.",
                VERDICT_BLOCK: "Agent: do not buy."}[self.verdict]
        if self.info_only:
            head = f"(information only, strategy not selected) {head}"
        return f"{head} {self.reason()}".strip()

    def to_row(self) -> dict[str, Any]:
        return {"id": f"{self.as_of_date}|{self.ticker}|{self.strategy}", "as_of_date": self.as_of_date,
                "ticker": self.ticker, "strategy": self.strategy, "verdict": self.verdict,
                "summary": self.summary()[:2000], "findings": [f.to_dict() for f in self.findings],
                "created_at": dt.datetime.now(dt.timezone.utc)}


def verdict_of(findings: list[Finding], block_on_unknown: bool) -> str:
    unknown_blocks = block_on_unknown and any(f.status == UNKNOWN and f.desk != "brain" for f in findings)
    if any(f.status == BLOCK for f in findings) or unknown_blocks:
        return VERDICT_BLOCK
    if any(f.status == WARN for f in findings):
        return CAUTION
    return GO


class Coordinator:
    def __init__(self, cfg: AgentConfig, costs: CostModel, exits: ExitRules,
                 desks: dict[str, Desk] | None = None, brain: Any = None):
        self.cfg = cfg
        self.costs = costs
        self.exits = exits
        self.desks = desks if desks is not None else default_desks(cfg, costs, exits)
        # brain(ticker, strategy, reason, findings) -> Finding, or None when the brain is off
        self.brain = brain
        self._cache: dict[tuple[str, str, dt.date], Finding] = {}

    def brief(self, day: dt.date, ticker: str, strategy: Strategy, history: pd.DataFrame,
              info_only: bool = False, reason: str = "") -> Brief:
        findings = []
        for name in self.cfg.desks:
            desk = self.desks.get(name)
            if desk is None:
                continue
            # news / events / insider do not depend on the strategy: ask once per stock per day
            key = (name, ticker if name != "odds" else f"{ticker}|{strategy.name}", day)
            if key not in self._cache:
                try:
                    self._cache[key] = desk(ticker, day, strategy, history)
                except Exception as exc:  # a desk must never fail the run
                    log.warning("Desk %s failed for %s: %s", name, ticker, exc)
                    self._cache[key] = unknown(name, f"{type(exc).__name__}: {exc}")
            findings.append(self._cache[key])
        if self.brain is not None and not info_only:   # the LLM is spent only on buys that would happen
            key = ("brain", f"{ticker}|{strategy.name}", day)
            if key not in self._cache:
                try:
                    self._cache[key] = self.brain(ticker, strategy, reason, list(findings))
                except Exception as exc:
                    log.warning("Brain failed for %s: %s", ticker, exc)
                    self._cache[key] = unknown("brain", f"{type(exc).__name__}: {exc}")
            findings.append(self._cache[key])
        b = Brief(day, ticker, strategy.name, verdict_of(findings, self.cfg.block_on_unknown), findings, info_only)
        log.info("Agent brief %s %s (%s): %s", ticker, strategy.name, b.verdict, b.reason())
        return b


def make_brain(raw: dict[str, Any] | None) -> Callable[..., Finding] | None:
    """The Claude reviewer as a callable, or None when disabled / no API key (logged once)."""
    from papertrader.agent.brain import BrainConfig, brain_available, brain_finding, reset_run_counter, review_brief

    cfg = BrainConfig.from_dict(raw or {})
    ok, why = brain_available(cfg)
    if not ok:
        if cfg.enabled:
            log.warning("Brain enabled in config but unavailable: %s", why)
        return None
    reset_run_counter()

    def review(ticker: str, strategy: Strategy, reason: str, findings: list[Finding]) -> Finding:
        return brain_finding(review_brief(ticker, strategy.label, reason, findings, cfg=cfg), cfg)

    return review


def default_desks(cfg: AgentConfig, costs: CostModel, exits: ExitRules) -> dict[str, Desk]:
    """The real desks, imported lazily so one broken data source cannot break the others."""

    def events(ticker, day, strategy, history):
        from papertrader.agent.events import blackout_check
        return blackout_check(ticker, on=day, days_before=cfg.results_days_before,
                              days_after=cfg.results_days_after)

    def news(ticker, day, strategy, history):
        from papertrader.agent.news import news_check
        return news_check(ticker, hours=cfg.news_hours)

    def insider(ticker, day, strategy, history):
        from papertrader.agent.insider import insider_check
        return insider_check(ticker, days=cfg.insider_days, today=day)

    def analyst(ticker, day, strategy, history):
        from papertrader.agent.analyst import analyst_check
        return analyst_check(ticker)

    def odds(ticker, day, strategy, history):
        from papertrader.agent.timemachine import odds_check
        return odds_check(history, strategy, exits, costs, lookback_days=cfg.odds_lookback_days,
                          min_signals=cfg.odds_min_signals, min_win_rate=cfg.odds_min_win_rate, ticker=ticker)

    return {"events": events, "news": news, "insider": insider, "analyst": analyst, "odds": odds}
