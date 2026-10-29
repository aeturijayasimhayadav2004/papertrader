---
name: nse-agent
description: >-
  The PaperTrader trading agent: runs the whole desk chain for one idea or explains the daily agent. Use for 'run the agent on INFY', 'full check before I buy', 'what did the agent decide today', 'brief me on X'.
argument-hint: "TICKER [strategy]"
---

# nse-agent

One trade, end to end (each step reads what the last wrote in data/agent/):

```
nse-scanner -> nse-news / nse-events / nse-insider / nse-analyst -> nse-entry -> nse-exit
  -> nse-timemachine -> nse-budget -> nse-risk -> nse-broker (exact yes) -> nse-alerts
```

Daily-strategy brief (the same check the daily GitHub Actions run does before every buy):

```bash
.venv/Scripts/python scripts/agent.py brief INFY --strategy ma_crossover
.venv/Scripts/python scripts/agent.py brief INFY --strategy supertrend --desks events,news,insider,analyst,odds
```

The daily run (scripts/run_daily.py) writes briefs and the chart scan to Supabase (tables
agent_briefs, agent_scans); the dashboard's Agent tab and the Telegram summary show them. Desk
BLOCK = no buy; WARN = buy with caution; unknown (data unreachable) does not block unless
agent.block_on_unknown is true in config.yaml. See docs/AGENT.md.

## Rules

- PAPER MONEY ONLY. Nothing in this project sends a real order; never suggest a workaround.
- Not financial advice. Report what the data says, in plain words, with Rs amounts and IST times.
- Free data (Yahoo, Google News, niftyindices.com) can lag or fail. NSE's own API blocks scripts, so
  NSE-only data may show as unavailable. Say so instead of guessing.
- Run commands from the project root. If the venv is missing: `py -3.11 -m venv .venv` then
  `.venv/Scripts/pip install -r requirements.txt && .venv/Scripts/pip install -e . --no-deps`.
