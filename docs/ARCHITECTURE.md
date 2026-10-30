# Architecture and roadmap: paper now, live later

PaperTrader trades **paper money only**. This document records which techniques were
borrowed from established trading platforms, how the code is laid out so the same logic
can later drive a real broker, and the gated steps required before any real order is sent.

## 1. What was studied and what was adopted

Sources were the public code and docs of open-source engines plus the public feature
docs of Indian retail platforms. Where the source code is public it was read directly.
REA (github.com/morluto/rea) was not needed: its own guidance is to skip reverse
engineering when source is available, and decompiling closed broker apps would breach
their terms.

| Technique | Seen in | Where it lives here |
|---|---|---|
| Same strategy and risk code for backtest, paper and live | QuantConnect Lean, NautilusTrader | `strategies/`, `exits.py`, `costs.py` are shared by `backtest.py` and `broker.py`; `live/interface.BrokerSession` for real brokers |
| Itemised Indian charges (STT, stamp duty, exchange, SEBI, GST, DP) plus slippage | Zerodha/Upstox brokerage calculators, Lean fee models | `costs.py` (`costs.model: india_delivery`) |
| ATR-based stops, trailing stop, take-profit (ROI) | Freqtrade (`stoploss`, `trailing_stop`, `minimal_roi`), Turtle rules | `exits.py`, `risk:` config |
| Volatility / risk-per-trade position sizing | Turtle rules, Lean portfolio construction | `risk.py` (`sizing: risk_based`) |
| Protections: stop-loss guard, max drawdown, cooldown, low-profit lock | Freqtrade protections | `protections.py`, `protections:` config |
| Market-regime filter (index above its 200-day average) | Common in Lean/Quantopian-style algos and Streak scanners | `backtest.market_regime`, `regime:` config |
| Supertrend, Bollinger mean reversion, time-series momentum | Streak / Tradetron templates (Supertrend is the most used Indian retail indicator), academic momentum | `strategies/` |
| Walk-forward validation | Jesse, Freqtrade (`--timerange` folds), Lean optimisation | `selection.py` (`method: walk_forward`) |
| Sortino, Calmar, profit factor, expectancy, drawdown duration | Freqtrade / Jesse reports | `metrics.py` |
| Monte Carlo resampling of trades | Jesse, many quant tool kits | `metrics.monte_carlo`, `scripts/run_backtest.py` |
| Pre-trade risk checks: order-rate cap, price-jump / bad-data guard | NautilusTrader RiskEngine, Hummingbot | `risk.py`, `runner._data_sanity` |
| Operator kill switch | Freqtrade `/stopentry`, Hummingbot kill switch | env `PAPERTRADER_KILL_SWITCH`, file `KILL_SWITCH`, GitHub repo variable |
| Telegram alerts | Freqtrade Telegram bot | `notify.py` |
| Desk-based agent: scanner, news, results blackout, insider deals, entry timer, R-multiple exits, session time machine, budget, risk PASS/BLOCK, approval-gated tickets, alerts | day-trader-skill (Claude skill pack, US), rebuilt for NSE | `agent/`, `scripts/agent.py`, `.claude/skills/nse-*`, [AGENT.md](AGENT.md) |

Not adopted, on purpose: hyper-parameter optimisation on the selection data (it would
make the out-of-sample numbers meaningless), intraday/HFT features (the data is daily
bars), leverage, shorting and F&O.

## 2. Layers

```
data.py (Yahoo daily bars, cached)
   │
strategies/*  ──► signals (BUY/SELL/HOLD + reason), causal, lookahead-tested
   │
selection.py  ──► which strategies may trade (holdout or walk-forward vs buy-and-hold)
   │
agent/        ──► desks brief every buy candidate (events, news, insider, odds); BLOCK skips it
   │
runner.py     ──► per day: fills → exits → mark/trail → buy blockers → decisions → persist
   │   uses: risk.py (size, limits) · protections.py · exits.py · costs.py
   │
live/         ──► BrokerSession contract, SimulatedExchange, OrderExecutor (paper | shadow | live, locked), reconcile, preflight
   │
storage/      ──► SQLite (always) + Supabase (primary in CI) + outbox
dashboard/    ──► Streamlit, read-only key
notify.py     ──► Telegram
```

Blockers for new buys, in order: daily-loss halt, kill switch, global protection lock,
market regime, then per-stock locks (cooldown, low-profit) and the data-sanity check.
Exits are never blocked.

## 3. Paper → live: gated steps

Live trading is refused by config today (`trading.mode` accepts only `paper`). Each
stage below must run cleanly for the stated time before the next starts.

**Stage A. Paper (now).** Daily GitHub Actions run. Goal: at least 3 months of live paper
results, because the backtest test period was used to choose strategies and is no longer
a clean test.

**Stage B. Shadow mode (planned).** A broker adapter logs in, reads funds, holdings and
the order book, and *computes* the orders it would place, but never sends them. Each
day it reconciles broker holdings against PaperTrader's positions and alerts on any
difference. Built: `live/` (BrokerSession contract, SimulatedExchange, OrderExecutor shadow mode, reconcile, scripts/preflight.py). Still needed: `live/brokers/<name>.py` for the chosen broker, a
`reconcile()` step in the runner, and `trading.mode: shadow`.

**Stage C. Small live (planned).** Real orders, with a hard capital cap (for example
Rs 25,000), one order per stock per day, and the kill switch tested first. Order rules:
LIMIT orders priced from the latest quote with a small buffer (or broker market orders
with price protection), rounded to the NSE tick size, each tagged with a client order ID
so a retried run can never double-send.

### Constraints that shape stages B and C (India, 2026)

* **SEBI retail algo framework (in force from 1 April 2026):** API access is tied to a
  registered **static IP**. Sessions need OAuth plus 2FA and expire daily. Orders are
  tagged by the broker. Below 10 orders per second no exchange registration is needed;
  this bot sends a handful per day. Check the current SEBI/NSE circulars and your broker's
  rules before going live; brokers apply them differently (for example, some convert
  market orders to price-protected orders).
* **GitHub Actions has no static IP**, so live order placement cannot run there. The
  free option is an **Oracle Cloud Always Free** VM (its reserved public IP is free)
  running the same `scripts/run_daily.py` from cron. Paper and shadow runs can stay on
  GitHub Actions.
* **Daily login:** the access token expires every day and needs 2FA. Plan for a manual
  login step each morning (or whatever flow your broker officially supports). Do not
  automate around 2FA in ways the broker's terms forbid.
* **Broker APIs (verify current pricing on the broker's developer page):** Zerodha
  Kite Connect "Personal" is free for orders but has no market data. Upstox, Dhan,
  Fyers, Angel One SmartAPI and Shoonya advertise free order APIs, with varying data
  terms and per-order fees. Recommended path: keep Yahoo daily bars for signals (free)
  and use a free broker API only for orders, holdings and quotes for limit prices.
* **Taxes:** delivery equity gains are taxed as short-term or long-term capital gains;
  keep the `trades` table as the audit trail.

## 4. Free stack

| Need | Choice | Cost |
|---|---|---|
| Daily data | yfinance (Yahoo) | free |
| Scheduler (paper/shadow) | GitHub Actions cron | free for public repos, 2,000 min/month private |
| Scheduler (live, static IP) | Oracle Cloud Always Free VM + cron | free |
| Database | Supabase free tier, with SQLite fallback | free |
| Dashboard | Streamlit Community Cloud | free |
| Alerts | Telegram bot | free |
| Broker orders (later) | A free retail order API (Kite Connect Personal, Upstox, Dhan, ...) | free subscription; brokerage and taxes still apply |
