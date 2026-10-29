---
name: nse-risk
description: >-
  Risk Manager. Checks every draft ticket against the limits: max loss per trade, daily loss limit, trades per day, reward:risk, entry window (09:20-14:30 IST) and market open (NSE holidays), position size, cash, move vs previous close (circuit-band proxy), no shorts, paper mode. PASS or BLOCK with the reason. Use for 'check my order', 'is this within my limits'.
---

# nse-risk

```bash
.venv/Scripts/python scripts/agent.py risk
.venv/Scripts/python scripts/agent.py risk --max-loss 500 --max-daily-loss 1500 --max-trades 2 --min-rr 1.5
```

Only a PASS ticket can be approved. Never loosen a limit to make a ticket pass unless the user asks
for that exact change.

## Rules

- PAPER MONEY ONLY. Nothing in this project sends a real order; never suggest a workaround.
- Not financial advice. Report what the data says, in plain words, with Rs amounts and IST times.
- Free data (Yahoo, Google News, niftyindices.com) can lag or fail. NSE's own API blocks scripts, so
  NSE-only data may show as unavailable. Say so instead of guessing.
- Run commands from the project root. If the venv is missing: `py -3.11 -m venv .venv` then
  `.venv/Scripts/pip install -r requirements.txt && .venv/Scripts/pip install -e . --no-deps`.
