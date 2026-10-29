---
name: nse-news
description: >-
  News Monitor for NSE stocks. Pulls the last hours of headlines (Google News India, plus NSE announcements when reachable), tags what moves a stock (results, dividend, order win, deal, fundraise, downgrade, target cut, guidance cut, SEBI/RBI action, raid, auditor/CFO exit, fraud/default) and says whether it would block a buy. Use for 'any news on INFY', 'why is X moving', 'check headlines'.
argument-hint: "TICKER [hours]"
---

# nse-news

```bash
.venv/Scripts/python scripts/agent.py news INFY --hours 24
```

BLOCK = fraud/default, a serious regulatory action (raid, ban, asset attachment) or an auditor/CFO
exit. WARN = downgrade, target cut, guidance cut, a fine / notice / probe. Summarise the 3-5 headlines
that matter, newest first, with their times. Keyword tags can misfire; read the titles.

## Rules

- PAPER MONEY ONLY. Nothing in this project sends a real order; never suggest a workaround.
- Not financial advice. Report what the data says, in plain words, with Rs amounts and IST times.
- Free data (Yahoo, Google News, niftyindices.com) can lag or fail. NSE's own API blocks scripts, so
  NSE-only data may show as unavailable. Say so instead of guessing.
- Run commands from the project root. If the venv is missing: `py -3.11 -m venv .venv` then
  `.venv/Scripts/pip install -r requirements.txt && .venv/Scripts/pip install -e . --no-deps`.
