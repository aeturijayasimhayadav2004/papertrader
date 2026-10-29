---
name: nse-entry
description: >-
  Entry Timer for NSE intraday setups. Watches the opening-range breakout on 5-minute bars and says waiting / triggered / expired, with the exact bar and price. Use for 'is INFY triggered', 'tell me when to get in', 'watch the breakout'.
argument-hint: "TICKER [HH:MM]"
---

# nse-entry

```bash
.venv/Scripts/python scripts/agent.py entry INFY                 # now
.venv/Scripts/python scripts/agent.py entry INFY --at 10:15      # as of 10:15 IST today
```

Triggered = the first COMPLETED 5-minute bar closing above the 15-minute opening-range high; a bar
still forming never counts. Expired = no trigger by 14:30 IST, or a close below the range low first.
The result is saved to data/agent/entry.json for /nse-exit. To watch live, re-run every few minutes.

## Rules

- PAPER MONEY ONLY. Nothing in this project sends a real order; never suggest a workaround.
- Not financial advice. Report what the data says, in plain words, with Rs amounts and IST times.
- Free data (Yahoo, Google News, niftyindices.com) can lag or fail. NSE's own API blocks scripts, so
  NSE-only data may show as unavailable. Say so instead of guessing.
- Run commands from the project root. If the venv is missing: `py -3.11 -m venv .venv` then
  `.venv/Scripts/pip install -r requirements.txt && .venv/Scripts/pip install -e . --no-deps`.
