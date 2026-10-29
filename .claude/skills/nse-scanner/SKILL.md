---
name: nse-scanner
description: >-
  Chart Scanner for NSE stocks. Scans Nifty 50 / 100 / 500 daily charts for breakouts on volume, bull flags, oversold bounces, gaps, 20-day-average reclaims and 52-week highs, or intraday opening-range breakouts on 5-minute bars. Use when the user says 'scan the market', 'find setups', 'what's breaking out', 'opening range breakouts', 'scan Nifty'.
argument-hint: "[universe] [--orb]"
---

# nse-scanner

Scan, then show the best hits first with the rule each one matched. A match is a place
to look, not a buy signal.

```bash
.venv/Scripts/python scripts/agent.py scan --universe nifty50                   # daily setups (completed daily bars)
.venv/Scripts/python scripts/agent.py scan --universe nifty500 --limit 40
.venv/Scripts/python scripts/agent.py scan --tickers INFY TCS RELIANCE
.venv/Scripts/python scripts/agent.py scan --orb --universe nifty50 --at 10:15  # opening-range breakouts so far today (IST)
```

Opening range = the first 15 minutes from 09:15 IST; a hit needs a completed 5-minute bar to close
beyond it. Next steps for a hit: /nse-news, /nse-events, then /nse-entry.

## Rules

- PAPER MONEY ONLY. Nothing in this project sends a real order; never suggest a workaround.
- Not financial advice. Report what the data says, in plain words, with Rs amounts and IST times.
- Free data (Yahoo, Google News, niftyindices.com) can lag or fail. NSE's own API blocks scripts, so
  NSE-only data may show as unavailable. Say so instead of guessing.
- Run commands from the project root. If the venv is missing: `py -3.11 -m venv .venv` then
  `.venv/Scripts/pip install -r requirements.txt && .venv/Scripts/pip install -e . --no-deps`.
