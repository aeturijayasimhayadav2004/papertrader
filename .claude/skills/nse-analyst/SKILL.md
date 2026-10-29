---
name: nse-analyst
description: >-
  Earnings Analyst for NSE companies. Reads the reported quarterly numbers (revenue, gross / operating / net margin, net income, diluted EPS) and compares the latest quarter with the same quarter a year earlier and the previous quarter, plus trailing twelve months. Use for 'how were Tata Motors results', 'is revenue growing', 'compare margins', 'sum up the quarter'. If the user pastes an earnings call transcript, summarise what changed.
argument-hint: "TICKER"
---

# nse-analyst

```bash
.venv/Scripts/python scripts/agent.py analyst TCS
```

Source: Yahoo's quarterly statements (usually only ~5 quarters for Indian stocks; some companies,
e.g. Infosys, report in USD there). Missing quarters are skipped, never guessed. For the full filing,
point the user to the company's results on nseindia.com.

## Rules

- PAPER MONEY ONLY. Nothing in this project sends a real order; never suggest a workaround.
- Not financial advice. Report what the data says, in plain words, with Rs amounts and IST times.
- Free data (Yahoo, Google News, niftyindices.com) can lag or fail. NSE's own API blocks scripts, so
  NSE-only data may show as unavailable. Say so instead of guessing.
- Run commands from the project root. If the venv is missing: `py -3.11 -m venv .venv` then
  `.venv/Scripts/pip install -r requirements.txt && .venv/Scripts/pip install -e . --no-deps`.
