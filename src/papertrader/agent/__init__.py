"""The trading agent: specialist desks plus a coordinator. PAPER TRADING ONLY.

Each desk does one job and returns `Finding`s (see types.py). Desks never raise
on network trouble: a source that cannot be reached gives status "unknown".

  market.py      NSE clock, sessions and holidays
  http.py        polite fetching (retries, timeouts, NSE cookie session)
  universe.py    Nifty 50 / 100 / 500 constituents
  intraday.py    5-minute / 1-minute bars
  scanner.py     daily setups and opening-range breakouts
  entry.py       entry timer (waiting / triggered / expired)
  news.py        headlines per stock, tagged
  events.py      board meetings / results dates -> blackout
  analyst.py     quarterly results, year on year
  insider.py     insider (SEBI PIT) trades and bulk / block deals
  exitplan.py    stop and target (R multiples), flat-by time
  timemachine.py session replays and the signal's past odds
  budget.py      position size from max loss
  riskcheck.py   PASS / BLOCK per order ticket
  tickets.py     order tickets; nothing executes without an explicit yes
  alerts.py      price / stop / target / time alerts
  orchestrator.py one brief per buy candidate, used by the daily runner
"""
