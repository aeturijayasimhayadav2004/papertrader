---
name: nse-events
description: >-
  Results calendar / blackout for NSE stocks. Finds the next board meeting for financial results (NSE when reachable, else Yahoo's earnings date) and says whether a new trade is in the blackout window (2 trading days before to 1 after). Use for 'when are results', 'does it report this week', 'earnings date'.
argument-hint: "TICKER"
---

# nse-events

```bash
.venv/Scripts/python scripts/agent.py events RELIANCE
.venv/Scripts/python scripts/agent.py events TCS --days-before 3 --days-after 1
```

Results move stocks more than anything else and a stop cannot protect against a gap, so the daily
agent skips buys inside the window. Yahoo dates may be estimates; say which source was used.

## Rules

- PAPER MONEY ONLY. Nothing in this project sends a real order; never suggest a workaround.
- Not financial advice. Report what the data says, in plain words, with Rs amounts and IST times.
- Free data (Yahoo, Google News, niftyindices.com) can lag or fail. NSE's own API blocks scripts, so
  NSE-only data may show as unavailable. Say so instead of guessing.
- Run commands from the project root. If the venv is missing: `py -3.11 -m venv .venv` then
  `.venv/Scripts/pip install -r requirements.txt && .venv/Scripts/pip install -e . --no-deps`.
