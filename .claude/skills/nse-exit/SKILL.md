---
name: nse-exit
description: >-
  Exit Planner. Sets the stop and the target BEFORE entry (R multiples), rounded to the NSE Rs 0.05 tick, and the flat-by time (15:15 IST for intraday, before broker auto square-off). Use for 'where's my stop', 'set a target', 'plan the trade', or right after /nse-entry.
argument-hint: "TICKER"
---

# nse-exit

```bash
.venv/Scripts/python scripts/agent.py plan INFY --from-entry --stop or_mid --target-r 1            # from the triggered entry
.venv/Scripts/python scripts/agent.py plan INFY --entry 1146.5 --stop pct --stop-pct 0.8 --target-r 1.5
.venv/Scripts/python scripts/agent.py plan INFY --entry 1146.5 --stop price --stop-price 1130 --target-r 2 --style swing
```

Stop methods: or_mid / or_low (opening range), pct, atr (--atr VALUE), price. Writes
data/agent/plan.json. Show risk per share, reward per share and the R multiple.

## Rules

- PAPER MONEY ONLY. Nothing in this project sends a real order; never suggest a workaround.
- Not financial advice. Report what the data says, in plain words, with Rs amounts and IST times.
- Free data (Yahoo, Google News, niftyindices.com) can lag or fail. NSE's own API blocks scripts, so
  NSE-only data may show as unavailable. Say so instead of guessing.
- Run commands from the project root. If the venv is missing: `py -3.11 -m venv .venv` then
  `.venv/Scripts/pip install -r requirements.txt && .venv/Scripts/pip install -e . --no-deps`.
