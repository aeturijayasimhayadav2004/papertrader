# PaperTrader

**PAPER TRADING - NO REAL MONEY.** A bot that tests simple strategies on daily NSE data,
ranks them out-of-sample, and trades only those that beat buy-and-hold on a simulated broker.
No broker, no broker API keys, no real orders.

## How it works

1. **Data**: daily OHLCV (split/dividend adjusted) for 10 Nifty 50 stocks from Yahoo Finance, cached in `data/cache/`.
2. **Strategies** (`src/papertrader/strategies/`): 20/50 MA crossover, RSI(14) oversold bounce, 20-day breakout.
   Each returns BUY / SELL / HOLD with a plain-English reason.
3. **Backtest** (`backtest.py`): signal at the close of day t, fill at the open of day t+1, 0.2% cost per side,
   optional 5% stop-loss. The universe is split into equal-weight sleeves.
4. **Selection** (`selection.py`): train 2015–2021 (in-sample), test 2022→today (out-of-sample).
   A strategy trades only if its out-of-sample return beats equal-weight buy-and-hold net of costs,
   with at least 5 trades. If none qualify, the bot holds cash. Re-ranked every 30 days.
5. **Risk** (`risk.py`): max 10% of equity per position, 5% stop-loss, max 5 positions,
   no new buys for the day after a 2% daily loss.
6. **Paper broker** (`broker.py`): cash, positions, orders, fills at the next open.
7. **Daily runner** (`runner.py`, `scripts/run_daily.py`): processes every new completed trading day,
   logs every decision with its reason, writes to SQLite + Supabase.
8. **Dashboard** (`dashboard/`): Streamlit, read-only Supabase key, password-gated.

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
- **Costs**: 0.2% per side is a rough, conservative stand-in for brokerage, STT, fees and slippage.
- **Daily bars**: stop-losses assume a fill at the stop price (or at the open on a gap), and the 2%
  daily-loss halt takes effect at the close, blocking new buys for the next session.
- **Yahoo Finance** data can be late, revised or missing. The bot logs data warnings and skips stocks with no bar.
- With these three simple strategies, the current backtest finds **none** beating buy-and-hold
  out-of-sample, so the bot holds cash. That is the intended, honest outcome, not a bug.
