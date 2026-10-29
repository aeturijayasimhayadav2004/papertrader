---
name: nse-alerts
description: >-
  Price Alerts. Rings once when a price, the stop, the target, a % move, a time (e.g. flat by 15:15 IST) or the NSE opening bell hits; knows NSE hours and 2026 holidays; can send to Telegram. Alerts are information, never orders. Use for 'alert me when', 'watch my stop', 'is the market open', 'remind me to close by 3:15'.
argument-hint: "[ticker price]"
---

# nse-alerts

```bash
.venv/Scripts/python scripts/agent.py clock                                   # NSE open / closed / next open
.venv/Scripts/python scripts/agent.py alerts add-plan                         # stop, target, flat-by from data/agent/plan.json
.venv/Scripts/python scripts/agent.py alerts add --type above --ticker INFY --price 1160
.venv/Scripts/python scripts/agent.py alerts add --type pct_move --ticker SBIN --pct 3
.venv/Scripts/python scripts/agent.py alerts add --type time --at 15:15 --note "flat by close"
.venv/Scripts/python scripts/agent.py alerts check --telegram                 # rings what hit, once
.venv/Scripts/python scripts/agent.py alerts list
```

To watch, re-run `alerts check` every few minutes while the market is open (or use /loop).

## Rules

- PAPER MONEY ONLY. Nothing in this project sends a real order; never suggest a workaround.
- Not financial advice. Report what the data says, in plain words, with Rs amounts and IST times.
- Free data (Yahoo, Google News, niftyindices.com) can lag or fail. NSE's own API blocks scripts, so
  NSE-only data may show as unavailable. Say so instead of guessing.
- Run commands from the project root. If the venv is missing: `py -3.11 -m venv .venv` then
  `.venv/Scripts/pip install -r requirements.txt && .venv/Scripts/pip install -e . --no-deps`.
