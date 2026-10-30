"""Real-money readiness layer. NOTHING HERE SENDS A REAL ORDER TODAY.

* `interface`  - the broker-neutral contract a real broker adapter must implement
                 (`BrokerSession`), plus order / fund / holding records and NSE
                 tick-size and price-band helpers.
* `simulated`  - `SimulatedExchange`, an offline exchange that behaves like a real
                 broker (fills, partials, rejections, rate limit, daily session expiry)
                 for tests and rehearsals.
* `executor`   - `OrderExecutor`: paper | shadow | live. Shadow logs the exact orders it
                 would send and reconciles; live raises `LiveTradingLocked` unless every
                 gate in docs/GO_LIVE.md is met. No verified real adapter exists, so live
                 is unreachable.
* `reconcile`  - bot positions / cash vs broker holdings / funds, with severities.

There is no real broker adapter in this package. See live/brokers/README.md.
"""
from __future__ import annotations
