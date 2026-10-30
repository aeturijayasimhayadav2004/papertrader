# Going live: the staged plan

PaperTrader trades **paper money** today. This page is the plan for moving, slowly and
reversibly, to a small amount of real money. Every stage has a gate: you do not start
the next stage until the gate is met. Going back a stage is always allowed and costs
nothing.

What exists in code today:

| Piece | File | State |
|---|---|---|
| Broker-neutral contract | `src/papertrader/live/interface.py` | done |
| Offline exchange for rehearsal | `src/papertrader/live/simulated.py` | done |
| Paper / shadow / live executor with the live lock | `src/papertrader/live/executor.py` | done; live is locked |
| Reconciliation | `src/papertrader/live/reconcile.py` | done |
| Go-live checklist | `scripts/preflight.py` -> `data/live/preflight.json` | done |
| Real broker adapter | `src/papertrader/live/brokers/<broker>.py` | **not written** (see `brokers/README.md`) |
| `trading.mode: shadow / live` in config | `src/papertrader/config.py` | **not allowed yet** (only `paper`) |

Live orders need **all** of these at once (the executor checks every one, every run):

1. `trading.mode: live` in `config.yaml` (config.py must first be changed to allow it)
2. environment variable `PAPERTRADER_LIVE_CONFIRM=I-ACCEPT-REAL-MONEY-RISK`
3. a capital cap (default Rs 25,000) that money at the broker plus today's buys stays under
4. kill switch off (no `KILL_SWITCH` file, `PAPERTRADER_KILL_SWITCH` unset)
5. `data/live/preflight.json` under 24 hours old with every item PASS
6. a real broker adapter whose class says `verified = True`, logged in today

## Stage A: paper (now, at least 3 months)

The daily GitHub Actions run trades the paper account and stores a snapshot per day.

**Gate to leave Stage A** (all of them):

* at least **60 trading days** (about 3 months) of `daily_snapshots`, with no gaps
  you cannot explain
* total return not below -10% and max drawdown not worse than 20% (preflight checks
  these; they are the "not catastrophic" floor, not a target)
* the strategy still beats buy-and-hold of the benchmark over the period, after costs,
  or you can explain why not (e.g. a strong one-way market)
* at least 20 closed trades, so the numbers mean something
* no unexplained runner errors in the last 20 trading days
* you have read every losing trade's exit reason at least once

Do not tune the strategy to the paper results and then count the same months as proof:
any parameter change restarts the 3-month clock.

## Stage B: shadow (at least 1 month)

Needs: a broker account, the adapter written per `brokers/README.md` (test-plan
steps 1-3 done), and `trading.mode: shadow` allowed in config.

Each trading day the bot logs in through the adapter (read-only), computes the exact
orders it **would** send (`OrderExecutor("shadow")` writes them to
`data/live/audit.jsonl`), and reconciles the broker's holdings against the bot's
positions (`data/live/reconcile_latest.json`). `place_order` is never called.

During shadow, mirror the bot's paper positions in the real account by hand only if
you want to test reconciliation with non-empty holdings; otherwise expect "missing at
broker" for every paper position and treat that as known. The simplest clean way:
run shadow with a separate, small paper book that starts flat on the same day.

**Gate to leave Stage B**:

* at least **20 trading days** (1 month) of shadow runs, every one completed
* **zero unexplained reconciliation breaks** (no `critical` rows you could not explain
  the same day)
* the would-send orders look sane: right symbols, quantities, prices on tick, inside
  the price band, tags present
* morning login done by hand every day without problems
* the server's static IP registered with the broker; preflight's static IP and clock
  checks PASS on the server
* Telegram alerts arrive for runs and for reconciliation differences
* adapter test-plan steps 4-5 done, then `verified = True` committed

## Stage C: small live (Rs 25,000 cap, 1 strategy)

Needs: Stage B gate met, `trading.mode: live` allowed in config, preflight all PASS.

Rules for the first month:

* **capital cap Rs 25,000** (`ExecutorLimits.capital_cap`), max Rs 10,000 per order,
  max 10 orders per day, **one strategy only**
* delivery (CNC) only; no intraday (MIS) until a month of clean CNC runs
* protected LIMIT orders (0.5% buffer, tick-rounded), never bare market orders
* **manual approval per order at first:** the bot sends the proposed order to
  Telegram and waits for your "yes" before placing it. (This approval step is not
  built yet; build it before Stage C. Until it exists, place Stage C orders by hand in
  the broker app from the shadow log.)
* run preflight every morning before market open; the executor refuses if the report
  is older than 24 hours

**Gate to leave Stage C**: 3 months live with live results within the range of the
paper results (same sign of return, drawdown no worse than paper + 5 points), zero
reconciliation breaks, zero duplicate orders, every fill price within 0.5% of the
decision price, and at least one tested emergency stop (below) during market hours.

## Stage D: scale-up rules

* Raise the capital cap at most **once a month** and at most **2x** each time
  (25k -> 50k -> 1L ...). The preflight's capital cap item fails above Rs 25,000 on
  purpose: changing that limit is a deliberate code change, in its own commit.
* Any drawdown above **10% of the cap**, any reconciliation break or any duplicate
  order: drop back to the previous cap and to manual approval until explained.
* Add a second strategy only after 3 months of the first at the current cap, and give
  it its own cap.
* Turn off per-order manual approval only after 1 month at a cap without a single
  rejected approval.
* Never let the bot use money you cannot afford to lose; keep the rest of your savings
  in a different account.

## Daily operating routine (Stages B-D)

Before 09:00 IST:

1. Log in to the broker's API login page by hand (password + 2FA); save the access token
   on the server (see `brokers/README.md`, section 2).
2. On the server: `python scripts/preflight.py` -> every line PASS.
3. Read yesterday's reconciliation (`data/live/reconcile_latest.json` or the Telegram
   alert). Any `critical` row: switch the kill switch on and investigate first.
4. Check that funds in the broker app match what you expect.

During the day:

5. Approve or decline each proposed order in Telegram (Stage C).
6. Glance at the order book once around midday and once after 15:30.

After the close:

7. Read the run summary in Telegram; confirm holdings in the broker app match the bot.
8. Log out of the API session (or let it expire).

## Emergency stop

Any one of these stops new orders immediately:

1. **Kill switch file:** create `KILL_SWITCH` in the project folder on the server
   (`touch KILL_SWITCH`). New buys stop; the live executor refuses all orders.
2. **Environment:** set `PAPERTRADER_KILL_SWITCH=1` for the service and restart it.
3. **Remove the confirmation:** unset `PAPERTRADER_LIVE_CONFIRM`.
4. **Stop the service:** `sudo systemctl stop papertrader-daily.timer` (and any
   intraday service).
5. **At the broker:** log out of all API sessions / revoke the API app in the broker's
   developer console. This works even if the server is unreachable.

Then, in the broker's web or mobile app: cancel open orders and, if needed, exit
positions by hand. Write down what happened before switching anything back on.

## Tax and record keeping

* Keep the broker's **contract notes** (emailed daily) and the yearly **tax P&L** and
  **capital gains** reports from the broker's back office. These are the official
  records; the bot's logs are supporting evidence only.
* Keep `data/live/audit.jsonl` (every intent and decision) and the daily
  reconciliation files for at least 8 years (the usual Indian record retention period
  for income tax; check with your CA).
* Delivery (CNC) gains are capital gains: short-term if held 12 months or less,
  long-term above that (rates and exemption limits change with each Budget). Intraday
  (MIS) equity trading is speculative business income and may need a tax audit above
  certain turnover. **Ask a chartered accountant**; this page is not tax advice.
* Losses can be carried forward only if the return is filed on time.

## SEBI / exchange rules summary (verify against current circulars)

As understood in October 2026; check the latest SEBI circulars, NSE circulars and
your broker's API terms before Stage B and again before Stage C.

* **Retail algo framework (SEBI, effective 1 April 2026):** retail investors may use
  broker APIs for algorithmic orders; the broker is responsible for the algo and tags
  every API order with an algo / strategy identifier.
* **Static IP:** API order placement must come from a static IP registered with the
  broker (one or a small number per client).
* **Authentication:** OAuth-based login with 2FA; API sessions expire daily (logout
  before the next trading day).
* **Order rate:** below **10 orders per second** a retail client's algo does not need
  exchange registration; above it, the strategy must be registered through the broker.
  This bot sends a handful of orders a day and the executor caps it at 10 per day.
* **Open APIs / third-party platforms:** only broker-empanelled or registered
  providers may offer algos to others; running your own code for your own account is
  the "self-use" case. Do not share your API keys or run the bot for anyone else.
* **Market orders:** some brokers convert API market orders into limit orders with
  market protection, or block them. This bot uses protected LIMIT orders.
* **Price bands:** orders outside the day's price band (circuit) are rejected by the
  exchange; the executor rounds to the 0.05 tick and the adapter must pass the band.

If anything in this summary disagrees with a current circular or your broker's terms,
the circular and the broker win, and this page should be corrected.
