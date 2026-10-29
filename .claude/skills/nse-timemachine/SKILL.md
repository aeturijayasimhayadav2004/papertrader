---
name: nse-timemachine
description: >-
  Time Machine. Before risking anything, replays the rest of today's session 10,000 times from the stock's own 5-minute bars at the same time of day over the last ~58 sessions (fixed seed, repeatable), and re-runs the opening-range rule on every past session. For swing trades, replays a strategy's past daily signals on that stock. Use for 'what are the odds', 'replay this trade', 'simulate', 'is this a coin flip'.
argument-hint: "[TICKER --strategy NAME]"
---

# nse-timemachine

```bash
.venv/Scripts/python scripts/agent.py timemachine                         # intraday: uses data/agent/plan.json
.venv/Scripts/python scripts/agent.py timemachine --at 10:15              # as if it were 10:15 IST
.venv/Scripts/python scripts/agent.py odds INFY --strategy ma_crossover   # swing: this strategy's past signals on this stock
```

Report target-first %, stop-first %, still-open %, the median outcome in R and the historical
fired / target / stop counts, then say plainly whether it is a coin flip. Always add: this is a
simulation, not a prediction; results days, halts and news are not in the sample. After the close
there are no bars left to simulate today; use --at.

## Rules

- PAPER MONEY ONLY. Nothing in this project sends a real order; never suggest a workaround.
- Not financial advice. Report what the data says, in plain words, with Rs amounts and IST times.
- Free data (Yahoo, Google News, niftyindices.com) can lag or fail. NSE's own API blocks scripts, so
  NSE-only data may show as unavailable. Say so instead of guessing.
- Run commands from the project root. If the venv is missing: `py -3.11 -m venv .venv` then
  `.venv/Scripts/pip install -r requirements.txt && .venv/Scripts/pip install -e . --no-deps`.
