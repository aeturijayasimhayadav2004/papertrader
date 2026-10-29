---
name: nse-budget
description: >-
  Budget Manager. Sizes the planned trade from the max loss you accept (Rs) or a % of the account, capped by position size and cash, including NSE charges (intraday MIS or delivery CNC), and writes one draft bracket ticket. Use for 'how many shares', 'size it', 'I can lose Rs 500'.
argument-hint: "[max loss Rs]"
---

# nse-budget

```bash
.venv/Scripts/python scripts/agent.py size --max-loss 1000                       # account defaults to config capital
.venv/Scripts/python scripts/agent.py size --account 50000 --risk-pct 1 --max-position-pct 20
```

qty = max loss / risk per share, rounded down. Show the loss at the stop and the gain at the target
after charges. Next: /nse-risk.

## Rules

- PAPER MONEY ONLY. Nothing in this project sends a real order; never suggest a workaround.
- Not financial advice. Report what the data says, in plain words, with Rs amounts and IST times.
- Free data (Yahoo, Google News, niftyindices.com) can lag or fail. NSE's own API blocks scripts, so
  NSE-only data may show as unavailable. Say so instead of guessing.
- Run commands from the project root. If the venv is missing: `py -3.11 -m venv .venv` then
  `.venv/Scripts/pip install -r requirements.txt && .venv/Scripts/pip install -e . --no-deps`.
