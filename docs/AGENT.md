# The trading agent

PAPER TRADING ONLY. Nothing here sends a real order: `trading.mode` accepts only `paper`,
and the ticket code raises `LiveTradingRefused` for any other mode.

PaperTrader started as one daily bot: 6 strategies, 10 stocks, one rule set. The agent adds a
team of **desks**. Each desk does one job and reports a *finding* (`ok`, `info`, `warn`,
`block` or `unknown`). A **coordinator** asks the desks about every buy before it is placed.
The desks are modelled on a US day-trading Claude skill pack
(github.com/Jakeschincariol/day-trader-skill) and rebuilt for NSE: IST hours, NSE holidays,
Indian charges, Indian data sources and SEBI disclosures.

## Desks

| Desk | Module | What it does | Free data source |
|---|---|---|---|
| Market clock | `agent/market.py` | NSE sessions (09:15-15:30 IST) and the 2026 holidays; open / closed / next open | NSE holiday list (built in, update each December) |
| Universe | `agent/universe.py` | Nifty 50 / 100 / 500 constituents | niftyindices.com CSVs |
| Scanner | `agent/scanner.py` | Daily setups: breakout on volume, bull flag, 20-day reclaim, oversold bounce, gaps, near the 52-week high. Intraday opening-range breakouts | Yahoo daily and 5-minute bars |
| Entry timer | `agent/entry.py` | Opening-range breakout: forming / waiting / triggered / expired. Only completed bars count | Yahoo 5-minute bars |
| News | `agent/news.py` | Headlines tagged (results, order win, deal, downgrade, guidance cut, SEBI/RBI action, raid, auditor/CFO exit, fraud...) | Google News India RSS; NSE announcements when reachable |
| Events | `agent/events.py` | Next results board meeting; blackout from 2 trading days before to 1 after | NSE when reachable, else Yahoo earnings date |
| Analyst | `agent/analyst.py` | Quarter vs a year ago and vs the previous quarter; trailing twelve months | Yahoo quarterly statements |
| Insider | `agent/insider.py` | Promoter / insider trades (SEBI PIT), pledges, bulk / block deals; net selling above Rs 5 crore warns | NSE when reachable, else Yahoo insider feed |
| Exit plan | `agent/exitplan.py` | Stop and target in R multiples, Rs 0.05 tick, flat by 15:15 IST for intraday | - |
| Time machine | `agent/timemachine.py` | 10,000 replays of today's session from the same time-of-day bars of the last ~58 sessions, plus the ORB rule re-run on each past session. **Odds desk:** a strategy's past daily signals on this stock | own history |
| Budget | `agent/budget.py` | Shares = max loss / risk per share, capped by position % and cash, after NSE charges (`costs.india_intraday` / `india_delivery`) | - |
| Risk check | `agent/riskcheck.py` | PASS / BLOCK per ticket: max loss, daily loss, trades per day, reward:risk, entry window, market open, size, cash, move vs previous close, no shorts, paper only | - |
| Tickets | `agent/tickets.py` | Plain-words order ticket. Approval needs the exact answer `yes` after a PASS. Paper fills go to a separate ledger (`data/agent/paper_ledger.json`) | - |
| Alerts | `agent/alerts.py` | Price / stop / target / % move / time / opening-bell alerts, each ringing once; Telegram | Yahoo chart quote |
| Coordinator | `agent/orchestrator.py` | One brief per buy candidate; verdict GO / CAUTION / BLOCK | - |

The US skill's "Pelosi tracker" (US Congress trades) has no Indian equivalent. The closest public
signal in India is promoter and insider dealing disclosed under SEBI PIT, plus bulk and block deals,
so that is what the insider desk reads.

## How the daily run uses it (automatic)

For the latest trading day only, when a selected strategy says BUY and nothing else has
blocked it (risk halt, kill switch, protections, market regime, data check), the coordinator asks the
desks listed in `config.yaml` `agent.desks` (default `events, news, insider, odds`):

* **events BLOCK:** a results meeting falls inside the blackout window.
* **news BLOCK:** fraud or default, a serious regulator action (raid, ban, asset attachment), or an auditor or CFO exit.
  **WARN:** downgrade, target cut, guidance cut, or a fine, notice or probe.
* **insider WARN:** net promoter or insider selling, or a new pledge.
* **odds BLOCK:** this strategy has at least 8 past signals on this stock and won less than 40% of
  them, or its average trade after costs was zero or worse.

A BLOCK skips the buy and the decision row says why. A WARN lets the buy go ahead with the warning
in the reason. A desk that cannot read its data says `unknown`; that never blocks unless you set
`agent.block_on_unknown: true`. Buy signals from strategies that are not selected get an
**information-only** brief, so you can see the agent working even while the bot holds cash.
Catch-up days (missed runs) skip the desks, because today's news says nothing about last week.

After the last day, the chart scan runs over `agent.scan_universe` (Nifty 50 by default). It is
information only and never places an order.

Briefs go to the `agent_briefs` table and the scan to `agent_scans` (Supabase and SQLite). Both
appear in the dashboard's **Agent** tab and in the Telegram summary.

## How you use it (on demand, with Claude)

The Claude Code skills in `.claude/skills/nse-*` call `scripts/agent.py`. Open this folder in Claude
Code and type, for example, `/nse-scanner`, `/nse-news INFY` or `/nse-agent RELIANCE`, or just
ask "scan Nifty for breakouts". One intraday idea, end to end:

```
/nse-scanner -> /nse-news, /nse-events, /nse-insider -> /nse-entry -> /nse-exit
  -> /nse-timemachine -> /nse-budget -> /nse-risk -> /nse-broker (you type yes) -> /nse-alerts
```

The same steps work without Claude: `python scripts/agent.py --help`.

## Limits, honestly

* **NSE's own API blocks scripts** (Akamai bot protection); it returned 403 from a home PC. The
  desks fall back to Google News and Yahoo, which are slower and less complete. NSE parsers are
  written to NSE's documented JSON shape but have not been confirmed against a live response.
* **Yahoo earnings dates can be estimates**, and its insider feed lags.
* **Intraday is on demand only.** GitHub Actions cron starts hours late, so it cannot run an
  intraday loop. Automatic intraday paper trading needs an always-on host (the Oracle Cloud Always
  Free VM in docs/ARCHITECTURE.md).
* **The time machine is a simulation, not a prediction.** It is built from about 58 sessions of
  the stock's own bars, and results days, halts and news are not in that sample.
* **Keyword news tagging can misfire.** Read the headlines it quotes.
* **2027 NSE holidays are not published yet.** Add them to `agent/market.py` in December.

## Rotation strategy (a strategy that passed the test)

The per-stock strategies never beat buy-and-hold, so the bot held cash. `portfolio/` adds
portfolio-level ("rotation") strategies that rank the whole universe each month-end and hold the
best few. Seven candidates were tested, with parameters fixed in advance from published research and
nothing tuned (`scripts/research_rotation.py`, report in `reports/rotation_research.md`).

* On the **Nifty 100**, four momentum variants passed the same bar as before: they beat equal-weight
  buy-and-hold out-of-sample (2022 to 2026) net of NSE charges and won 3 of 5 yearly folds.
* On the **Nifty 50**, none passed (2 of 5 folds).
* **Caveat:** the test uses today's index members for the past, which flatters momentum
  (survivorship bias), and 5 folds are weak evidence.

Paper-traded: `momentum_12_1_regime` on the Nifty 100. At each month-end it holds the top 10 by
12-month return (skipping the latest month), in equal weights, and holds cash while Nifty is below its
200-day average. It had the best risk-adjusted return (Sharpe 1.34 vs 1.29 for buy-and-hold) and the
smallest drawdown of the four that passed. It runs as a **separate Rs 10 lakh paper account**
(`portfolio/paper.py`, table `rotation_snapshots`, dashboard **Rotation** tab), so its results can be
compared honestly with the research numbers. Change it under `portfolio:` in config.yaml.

## The brain (optional): Claude reviews each brief

When `brain.enabled` is true and an API key exists, Claude reads all desk findings for a buy
candidate and returns agree, veto or abstain, with a confidence and up to three sentences of
reasoning (desk `brain`).

**Veto only.** The brain can stop a buy, never start one, and never changes size, stop or target.
A veto with confidence >= `veto_min_confidence` (0.7) is a BLOCK; a weaker veto is a WARN; agree and
abstain are information. Any failure (no key, network, refusal, bad reply, budget used up) gives no
view, so the bot runs exactly as before, even with `block_on_unknown`. Headlines and filings are
untrusted web text: they go to Claude inside one delimited JSON block, and the system prompt tells it
to treat that block as data and ignore any instructions inside it.

**Enable it**
1. Create an API key at https://console.anthropic.com (Settings -> API keys) and add credit.
2. Local: put `ANTHROPIC_API_KEY=...` in `.env`. GitHub Actions: add a repository secret
   `ANTHROPIC_API_KEY` (the workflow already passes it to the run).
3. `config.yaml` `brain:` sets the model (`claude-opus-5-5` by default, or `claude-haiku-5-5`), the
   effort (`low`), `max_calls_per_run` and `monthly_budget_usd` (default 5). Spend is tracked in
   `data/agent/brain_usage.json`.

**Cost per brief (estimate):** about 2,000 tokens in and 400-1,500 out. `claude-opus-5-5` at effort
low costs about $0.02 (up to $0.04); `claude-haiku-5-5` costs about $0.0005. On Opus, if Claude's safety
filter declines a request, the API retries it on another Claude model automatically
(`fallbacks: default`); set `fallbacks: none` to turn that off.

## Learning from results

`agent/learning.py` and `scripts/learn.py`, run after every daily run (information only):

* **Live:** for every brief and scan setup, what the stock did 1, 5 and 20 trading days later and
  against the average stock (table `agent_outcomes`), summarised per desk, verdict, Claude decision
  and setup (table `agent_scorecard`).
* **History backfill:** the same scan rules replayed over 5 years of the Nifty 50, and the odds desk
  replayed on 3,400+ past strategy trades. It compares what the desk would have blocked with what it
  would have allowed.
* **Suggestions** in `reports/learning.md` give the evidence and sample size, and are never applied
  automatically (auto-tuning on the same data overfits). Rows with fewer than 20 cases are marked
  "too few to judge". The dashboard's **Learning** tab shows the scorecards.

First results (Nifty 50, 2021-2026):

* **Bull flag** is the only setup with a historical edge: +0.67% vs the average stock over 20 days,
  better in 4 of 6 years.
* **Breakout** and **near the 52-week high** lagged in most years.
* **Odds desk:** it would have blocked 61% of trades with almost no difference in average return
  overall (+1.39% blocked vs +1.35% allowed). It helped for momentum, MA crossover, Bollinger and RSI,
  and hurt for breakout and supertrend.
