---
name: nse-broker
description: >-
  Order Executor (PAPER). Shows a risk-passed ticket in plain words and records a paper fill ONLY after the user types yes for that exact ticket. No real broker is connected; live mode is refused in code. Use for 'place it', 'execute', 'buy it', 'show my tickets', 'close the trade'.
argument-hint: "[ticket id]"
---

# nse-broker

```bash
.venv/Scripts/python scripts/agent.py ticket show
.venv/Scripts/python scripts/agent.py ticket approve INFY-20261030-101500 --answer yes   # only after the user typed yes in chat
.venv/Scripts/python scripts/agent.py ticket fill INFY-20261030-101500 --price 1146.50
.venv/Scripts/python scripts/agent.py ticket close INFY-20261030-101500 --price 1160.30 --reason target
```

Procedure: show the ticket (`ticket show`), ask the user "Approve this exact paper order? Type yes",
and run `approve` only if their reply is exactly yes. Fill at the current price (or the limit if
lower). The paper ledger is data/agent/paper_ledger.json, separate from the daily bot's account.
Then offer /nse-alerts.

## Rules

- PAPER MONEY ONLY. Nothing in this project sends a real order; never suggest a workaround.
- Not financial advice. Report what the data says, in plain words, with Rs amounts and IST times.
- Free data (Yahoo, Google News, niftyindices.com) can lag or fail. NSE's own API blocks scripts, so
  NSE-only data may show as unavailable. Say so instead of guessing.
- Run commands from the project root. If the venv is missing: `py -3.11 -m venv .venv` then
  `.venv/Scripts/pip install -r requirements.txt && .venv/Scripts/pip install -e . --no-deps`.
