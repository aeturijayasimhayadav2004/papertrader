from __future__ import annotations

import datetime as dt

import pytest

from papertrader.agent.budget import bracket_ticket, size_position
from papertrader.agent.exitplan import plan_trade
from papertrader.agent.tickets import (LiveTradingRefused, Ticket, approve, close_paper, execute_paper,
                                       ledger_today, load_ledger, load_tickets, render, save_tickets)

NOW = dt.datetime(2026, 10, 29, 10, 15)


@pytest.fixture
def ticket():
    plan = plan_trade("INFY.NS", 1086.05, stop_method="or_mid", range_high=1084.0, range_low=1076.30)
    t = bracket_ticket(plan, size_position(plan, account=100_000, max_loss=50), now=NOW)
    t.status = "risk_passed"
    return t


def test_render_plain_words(ticket):
    text = render(ticket)
    assert "Buy 6 INFY at up to Rs 1,086.05" in text
    assert "Stop-loss Rs 1,080.15" in text and "target Rs 1,091.95" in text
    assert "Max loss" in text and "PAPER MONEY" in text and '"yes"' in text


@pytest.mark.parametrize("answer", ["y", "Yes please", "yes to all", "ok", "sure", "", "no", "yess", "YES!"])
def test_approve_rejects_anything_but_yes(ticket, answer):
    assert approve(ticket, answer, now=NOW).status == "risk_passed"
    assert ticket.approved_at is None


@pytest.mark.parametrize("answer", ["yes", "  YES ", "Yes\n"])
def test_approve_exact_yes(ticket, answer):
    approve(ticket, answer, now=NOW)
    assert ticket.status == "approved"
    assert ticket.approved_at.startswith("2026-10-29T10:15")


def test_approve_needs_risk_pass(ticket):
    ticket.status = "draft"
    assert approve(ticket, "yes").status == "draft"
    ticket.status = "rejected"
    assert approve(ticket, "yes").status == "rejected"


def test_live_mode_refused(ticket):
    d = ticket.to_dict()
    d["mode"] = "live"
    with pytest.raises(LiveTradingRefused):
        Ticket.from_dict(d)
    ticket.mode = "live"       # even if someone flips it by hand
    with pytest.raises(LiveTradingRefused):
        approve(ticket, "yes")
    with pytest.raises(LiveTradingRefused):
        execute_paper(ticket, 1086.0)


def test_store_roundtrip(tmp_path, ticket):
    path = tmp_path / "tickets.json"
    assert load_tickets(path) == []
    save_tickets(path, [ticket])
    assert load_tickets(path) == [ticket]


def test_execute_and_close_idempotent(tmp_path, ticket):
    ledger = tmp_path / "ledger.json"
    with pytest.raises(ValueError):
        execute_paper(ticket, 1086.0, now=NOW, ledger_path=ledger)    # not approved yet
    approve(ticket, "yes", now=NOW)
    with pytest.raises(ValueError):
        execute_paper(ticket, 1090.0, now=NOW, ledger_path=ledger)    # above the limit
    rec = execute_paper(ticket, 1086.0, now=NOW, ledger_path=ledger)
    again = execute_paper(ticket, 1086.0, now=NOW, ledger_path=ledger)
    assert rec == again and len(load_ledger(ledger)) == 1
    assert ticket.status == "executed" and rec["status"] == "open" and rec["entry_charges"] > 0

    closed = close_paper(ticket.ticket_id, 1091.95, "target", now=NOW.replace(hour=11), ledger_path=ledger)
    assert closed["gross_pnl"] == pytest.approx(6 * 5.95)
    assert closed["net_pnl"] < closed["gross_pnl"]
    assert closed["r"] == pytest.approx(5.95 / 5.85, abs=1e-3)
    assert close_paper(ticket.ticket_id, 1000, "stop", ledger_path=ledger)["exit_price"] == 1091.95
    stats = ledger_today(load_ledger(ledger), dt.date(2026, 10, 29))
    assert stats["trades_today"] == 1 and stats["realised_pnl_today"] == closed["net_pnl"]
    with pytest.raises(KeyError):
        close_paper("NOPE", 1.0, "x", ledger_path=ledger)
