# Adding a real broker adapter

There is **no real broker adapter in this repository**, on purpose. This page explains
how to write one when you have picked a broker. Until an adapter exists, passes every
step of the test plan below and is marked `verified = True`, the live executor refuses
to send anything (`LiveTradingLocked`).

Everything about broker APIs below was written in October 2026 from public
documentation. **APIs, fees, login rules and SEBI requirements change. Check the
broker's current developer docs and the latest SEBI / NSE circulars before you
write a line of adapter code.**

## 1. What an adapter is

One file, `src/papertrader/live/brokers/<broker>.py`, with one class that implements
the `BrokerSession` protocol in `papertrader/live/interface.py`:

```python
class ZerodhaSession:                     # example name only
    name = "zerodha"
    verified = False                      # flip to True only after section 6 passes

    def __init__(self, api_key: str, access_token: str): ...
    def session_status(self) -> SessionStatus: ...
    def funds(self) -> Funds: ...
    def holdings(self) -> list[Holding]: ...
    def positions(self) -> list[BrokerPosition]: ...
    def orders(self) -> list[BrokerOrder]: ...
    def quote(self, symbols: list[str]) -> dict[str, Quote]: ...
    def place_order(self, request: OrderRequest) -> BrokerOrder: ...
    def cancel_order(self, order_id: str) -> BrokerOrder: ...
    def order_status(self, order_id: str) -> BrokerOrder: ...
```

Rules:

* Use the broker's **official** SDK or documented REST API only. No screen scraping,
  no unofficial libraries that log in with your password.
* The adapter never logs in by itself. It receives an **access token** that you got
  from the broker's official login flow that morning (section 2).
* Never put credentials in the repo or in `config.yaml`. Read them from environment
  variables (or the server's secret store) at run time. `.env` is git-ignored.
* Every broker error becomes either a `BrokerOrder` with `status="REJECTED"` and a
  plain-words `reject_reason`, or `SessionExpired` for auth errors. Never swallow an
  error and return a fake success.
* Map broker statuses to exactly `OPEN | COMPLETE | REJECTED | CANCELLED | PARTIAL`.
  Unknown statuses map to `OPEN` and are logged, so the order is watched, not forgotten.

## 2. Daily login (OAuth + 2FA)

Under SEBI's retail algo framework (in force from 1 April 2026) API sessions need
OAuth plus two-factor authentication and expire daily. The usual flow:

1. Open the broker's login URL with your API key (app id) in a browser.
2. Log in by hand with your password and 2FA (TOTP / OTP).
3. The broker redirects to your registered redirect URL with a short-lived
   **request token** (or "auth code").
4. A small local script exchanges that request token plus your API secret for an
   **access token**, valid until the broker's daily expiry (often around 06:00 IST
   the next morning; some brokers use a different time).
5. Store the access token where the trading process can read it (an env file on the
   server, permissions 600). Delete it at the end of the day.

**Do not automate the 2FA step** with stored TOTP secrets, headless browsers or
password replay unless your broker explicitly documents and permits that for retail
API users. Many brokers' terms forbid it, and it removes the human check that the
daily login is meant to be. A one-minute manual login each morning is the design.

## 3. Static IP

SEBI's framework ties API order placement to a **static IP registered with the
broker**. GitHub Actions has no static IP, so live orders must run from a fixed
server (the plan in `docs/ARCHITECTURE.md` uses an Oracle Cloud Always Free VM with a
reserved public IP).

* Register that IP in the broker's developer console.
* Set `PAPERTRADER_STATIC_IP` on the server; `scripts/preflight.py` compares it with
  the public IP it sees (via api.ipify.org) and fails on any mismatch.
* Read-only calls (funds, holdings, quotes) may work from other IPs depending on the
  broker; order placement will not.

## 4. Order tagging and idempotency

* `OrderRequest.tag` (at most 20 characters) is set by the executor to the
  deterministic `client_order_id` (`PT` + 18 hex characters, a hash of
  date|ticker|side|strategy). Pass it in the broker's tag / correlation field.
  SEBI's algo ID tagging is applied by the broker; you do not invent an algo ID.
* Idempotency: before placing, look for an existing order today with the same tag
  (most brokers let you list today's orders). If one exists, return it instead of
  placing again. This is what `SimulatedExchange` does with `client_order_id`, and
  section 6 tests it.
* Stay far below 10 orders per second (above that, exchange registration of the algo
  is required). The executor's per-day cap defaults to 10 orders in total.

## 5. Field mapping (check each against current docs)

| OrderRequest | Zerodha Kite Connect | Upstox | Dhan | Fyers | Angel One SmartAPI |
|---|---|---|---|---|---|
| SDK (official) | `kiteconnect` (pykiteconnect) | `upstox-python-sdk` | `dhanhq` | `fyers-apiv3` | `smartapi-python` |
| symbol | `tradingsymbol` + `exchange="NSE"` | `instrument_token` (instrument key, e.g. `NSE_EQ\|<ISIN>`) | `security_id` + `exchange_segment` | `symbol="NSE:INFY-EQ"` | `tradingsymbol="INFY-EQ"` + `symboltoken` |
| side | `transaction_type` BUY/SELL | `transaction_type` BUY/SELL | `transaction_type` BUY/SELL | `side` 1 / -1 | `transactiontype` BUY/SELL |
| product CNC / MIS | `CNC` / `MIS` | `D` / `I` | `CNC` / `INTRADAY` | `CNC` / `INTRADAY` | `DELIVERY` / `INTRADAY` |
| order_type | `LIMIT`, `MARKET`, `SL`, `SL-M` | `LIMIT`, `MARKET`, `SL`, `SL-M` | `LIMIT`, `MARKET`, `STOP_LOSS`, `STOP_LOSS_MARKET` | `type` 1 limit, 2 market, 4 SL, 3 SL-M | `LIMIT`, `MARKET`, `STOPLOSS_LIMIT`, `STOPLOSS_MARKET` |
| validity | `DAY` | `DAY` | `DAY` | `validity="DAY"` | `duration="DAY"` |
| tag | `tag` (<= 20 chars) | `tag` | `correlationId` | `orderTag` | `ordertag` |
| login | request_token -> `generate_session` -> access_token | OAuth auth code -> token | access token from Dhan web / OAuth | auth code -> token | client code + PIN + TOTP (read the terms) |

Notes:

* Some brokers convert or restrict API **market** orders (for example, adding market
  protection). The executor sends protected LIMIT orders by default for this reason.
* Symbol lookup: most brokers need an instrument master file (daily CSV / JSON) to
  map `INFY` to their token or security id. Download it once per day and cache it.
* Quotes: `Quote.upper_circuit` / `lower_circuit` must come from the broker's quote
  (or the exchange's price band file). If the broker does not return them, leave them
  `None` and rely on the broker's own rejection.
* Kite Connect's free "Personal" plan has no market data; quotes then need another
  source, or the paid plan.

## 6. Test plan before `verified = True`

Do these in order. Do not skip a step because the previous one went well.

1. **Unit tests with recorded responses.** Save real (redacted) JSON responses from
   the broker's sandbox or docs into `tests/fixtures/<broker>/` and test every mapping:
   funds, holdings (incl. T1), positions (CNC and MIS), order book, each status, each
   rejection message, auth-expired errors -> `SessionExpired`.
2. **Behaviour parity with `SimulatedExchange`.** Write the adapter tests to mirror
   `tests/test_live_simulated.py`: tick rejection, circuit rejection, insufficient
   funds, CNC sell without holdings, outside market hours, MIS after 15:20, duplicate
   client_order_id returns the existing order, session expiry. Where the real broker
   behaves differently, document it in the adapter's docstring.
3. **Read-only against the real account.** With a real access token, call only
   `session_status`, `funds`, `holdings`, `positions`, `orders`, `quote`. Compare with
   the broker's web app by hand.
4. **Shadow mode for at least one month** (`OrderExecutor("shadow", session=adapter)`),
   every trading day, with zero reconciliation breaks (`docs/GO_LIVE.md`, Stage B).
5. **One manual, tiny live order** placed by you in the broker's web app, then read
   back through `order_status` / `orders` to confirm mapping of a real fill.
6. Only then set `verified = True` in the adapter class, in a commit of its own with
   the test evidence in the message, and run `scripts/preflight.py`.

`verified = True` alone unlocks nothing. Live still needs every gate in
`papertrader/live/executor.py` (config, confirmation env, capital cap, kill switch
off, fresh passing preflight, valid session).
