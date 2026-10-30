# PaperTrader

**PAPER TRADING - NO REAL MONEY.** A bot that tests simple strategies on daily NSE data,
ranks them out-of-sample, and trades only those that beat buy-and-hold on a simulated broker.
No broker, no broker API keys, no real orders.

## How it works

1. **Data**: daily OHLCV (split/dividend adjusted) for 10 Nifty 50 stocks from Yahoo Finance, cached in `data/cache/`.
2. **Strategies** (`src/papertrader/strategies/`): 20/50 MA crossover, RSI(14) oversold bounce, 20-day breakout,
   Supertrend(10,3), Bollinger dip-in-uptrend, 6-month momentum. Each returns BUY / SELL / HOLD with a
   plain-English reason, and each is lookahead-tested.
3. **Costs** (`costs.py`): itemised NSE delivery charges (STT, stamp duty, exchange, SEBI, GST, DP charge)
   plus a slippage allowance; or a flat % per side.
4. **Backtest** (`backtest.py`): signal at the close of day t, fill at the open of day t+1. Exits
   (`exits.py`: fixed or ATR stop, optional trailing stop and take-profit) and the market-regime filter are
   the same rules the paper broker uses. The universe is split into equal-weight sleeves.
5. **Selection** (`selection.py`): train 2015–2021 (in-sample), test 2022→today (out-of-sample).
   A strategy trades only if it beats equal-weight buy-and-hold out-of-sample net of costs (on return or
   Sharpe, configurable), with at least 5 trades, and in walk-forward mode also wins at least half of the
   yearly folds. If none qualify, the bot holds cash. Re-ranked every 30 days.
6. **Risk** (`risk.py`): max 10% of equity per position (or risk-based sizing), max 5 positions,
   max 5 new orders a day, no new buys for the day after a 2% daily loss.
7. **Protections** (`protections.py`, Freqtrade-style): stop-loss guard, drawdown pause, per-stock cooldown and
   low-profit lock. Plus a **market regime filter** (no new buys while Nifty is below its 200-day average),
   a bad-data guard and an operator **kill switch**. These only block new buys; exits always run.
8. **Paper broker** (`broker.py`): cash, positions, orders, fills at the next open. Implements the
   paper account. Real brokers plug in through `live/interface.BrokerSession` (see [docs/GO_LIVE.md](docs/GO_LIVE.md)).
9. **Daily runner** (`runner.py`, `scripts/run_daily.py`): processes every new completed trading day,
   logs every decision with its reason, writes to SQLite + Supabase, sends a Telegram summary if configured.
10. **Dashboard** (`dashboard/`): Streamlit, read-only Supabase key, password-gated.
11. **Trading agent** (`src/papertrader/agent/`): a team of desks asked before every buy:
    results-calendar blackout, news, insider / bulk-deal activity, and the signal's own past odds on that
    stock. A BLOCK skips the buy. It also runs a daily Nifty 50 chart scan, and offers on-demand intraday tools
    (opening-range entry timer, exit planner, 10,000-path time machine, sizing, risk check, paper tickets
    that need your exact "yes", alerts) through `scripts/agent.py` and the Claude Code skills
    `/nse-scanner`, `/nse-news`, `/nse-agent`, and others. See [docs/AGENT.md](docs/AGENT.md).

Where these techniques came from, and the gated plan for moving to a real broker later:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

All settings: `config.yaml`. Secrets: `.env` (bot) and Streamlit secrets (dashboard), never in git.

## Setup

```powershell
py -3.11 -m venv .venv          # or: py -V:Astral/CPython3.11.14 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -e . --no-deps
```

## Phase 1: data + strategies + backtester

```powershell
python -m pytest tests/test_backtest.py tests/test_strategies.py tests/test_data.py -v
python scripts/run_backtest.py            # --refresh to force re-download
```
Check: the table shows in-sample vs out-of-sample metrics for each strategy and buy-and-hold;
`reports/equity_in_sample.png`, `reports/equity_out_of_sample.png`, `reports/backtest_summary.csv` exist.
The lookahead tests prove (a) signals never change when future bars are removed, (b) changing future
prices never changes past results, (c) every fill is strictly after its signal, and (d) the checker
catches a deliberately cheating strategy.

## Phase 2: risk + paper broker + runner

```powershell
python -m pytest -v                       # all tests
python scripts/run_daily.py               # 1st run: processes the latest completed day
python scripts/run_daily.py               # 2nd run: "Already processed ... nothing to do"
```
Check: `logs/papertrader.log` has one line per stock × strategy with the reason; `data/papertrader.db`
row counts don't change on the second run. Inspect with any SQLite viewer or:
```powershell
python -c "import sqlite3;c=sqlite3.connect('data/papertrader.db');print([(t,c.execute(f'select count(*) from {t}').fetchone()[0]) for t in ['runs','decisions','orders','positions','trades','daily_snapshots','backtest_results']])"
```

## Phase 3 & 4: Supabase, dashboard, deployment, schedule

See [docs/DEPLOY.md](docs/DEPLOY.md).

## Alerts and kill switch

- **Telegram**: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (in `.env` locally, as repo secrets in GitHub
  Actions). Setup steps are at the top of `src/papertrader/notify.py`. With no token, nothing is sent.
- **Kill switch**: create a file named `KILL_SWITCH` in the project root, set `PAPERTRADER_KILL_SWITCH=1`, or
  set the GitHub repo variable `PAPERTRADER_KILL_SWITCH` to `1`. New buys stop; exits and stops keep running.

## Run-safety rules

- A day counts as "completed" only after 15:45 IST. Earlier runs ignore today's partial bar.
- State lives in the latest `daily_snapshots.state_json`. Re-running a processed day is a no-op;
  `--force` recomputes it from the previous snapshot and overwrites by primary key (no duplicates).
- Missed days (holidays, outages) are processed one by one, in order, on the next run.
- If Supabase is down, writes go to SQLite and an outbox, replayed on the next successful run.
  If Supabase is down **and** there is no local state, the bot refuses to trade (exit code 2).

## Known limitations (read before trusting any number)

- **Survivorship bias**: the universe is today's large caps, so the backtest favours past winners.
- **Selection bias**: the test period is used to choose strategies, so it is no longer fully unseen.
  Live paper results are the real out-of-sample test.
- **Costs**: the itemised model uses today's published rates plus 0.05% slippage. Rates change; check
  your broker's charges page. Backtest sizing is per sleeve, so risk-based sizing and the protections are
  only fully exercised by the daily runner, not by the backtest.
- **Monte Carlo** in the backtest report resamples trades on a 1/N sleeve and ignores overlap between
  trades. Treat it as a robustness check, not a forecast.
- **Daily bars**: stop-losses assume a fill at the stop price (or at the open on a gap), and the 2%
  daily-loss halt takes effect at the close, blocking new buys for the next session.
- **Yahoo Finance** data can be late, revised or missing. The bot logs data warnings and skips stocks with no bar.
- With these six strategies, the current backtest (data to Oct 2026) finds **none** beating buy-and-hold
  out-of-sample, mainly because they sit in cash about two-thirds of the time while large caps rose.
  So the bot holds cash. That is the intended, honest outcome, not a bug.
