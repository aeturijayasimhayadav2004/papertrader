---
name: nse-insider
description: >-
  Insider Tracker for NSE stocks, the Indian version of a Congress-trades tracker. Reads promoter / director / KMP trades disclosed under SEBI's insider-trading (PIT) rules, pledges, and bulk / block deals, and flags heavy promoter selling or new pledges. Use for 'are promoters selling', 'insider trades', 'bulk deals in X', 'pledged shares'.
argument-hint: "TICKER [days]"
---

# nse-insider

```bash
.venv/Scripts/python scripts/agent.py insider RELIANCE --days 30
```

WARN = net insider selling above Rs 5 crore, or a new / invoked pledge. ESOP allotments, gifts and
inter-se promoter transfers do not count as selling. Disclosures are public and can lag the trade by
days; NSE data is often unreachable from scripts, then Yahoo's slower feed is used.

## Rules

- PAPER MONEY ONLY. Nothing in this project sends a real order; never suggest a workaround.
- Not financial advice. Report what the data says, in plain words, with Rs amounts and IST times.
- Free data (Yahoo, Google News, niftyindices.com) can lag or fail. NSE's own API blocks scripts, so
  NSE-only data may show as unavailable. Say so instead of guessing.
- Run commands from the project root. If the venv is missing: `py -3.11 -m venv .venv` then
  `.venv/Scripts/pip install -r requirements.txt && .venv/Scripts/pip install -e . --no-deps`.
