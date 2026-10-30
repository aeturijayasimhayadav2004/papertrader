# Rotation strategy research (PAPER TRADING ONLY)

Generated 2026-10-29 23:58. Costs: NSE delivery (itemised), about 0.34% per round trip on Rs 1 lakh. Capital Rs 1,000,000. Rebalance band 1% of equity. Cash earns 0.

**The bar** (same as the per-stock strategies): out-of-sample, net of costs, beat equal-weight buy-and-hold of the same universe on total return AND win at least 50% of the 12-month walk-forward folds. All parameters were fixed in advance from published research / index rules; **no parameter was tuned, on either period**, and no variant was added after seeing results.

## nifty100 (100 stocks, today's constituents)

In-sample 2015-01-01 to 2021-12-31; out-of-sample 2022-01-01 to 2026-10-29.

| Benchmark | IS CAGR | IS Sharpe | IS max DD | OOS CAGR | OOS Sharpe | OOS max DD | OOS total |
|---|---|---|---|---|---|---|---|
| Equal-weight buy & hold (net of costs) | +24.1% | 1.35 | -37.2% | +22.4% | 1.29 | -19.9% | +165.1% |
| Nifty 50 index ^NSEI (price, no costs) | +11.1% | 0.70 | -38.4% | +5.9% | 0.49 | -16.5% | +31.9% |

| Strategy | IS CAGR | IS Sharpe | IS max DD | OOS CAGR | OOS Sharpe | OOS max DD | OOS excess vs B&H | OOS excess vs Nifty | OOS folds won | Overfit? | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 12-1 momentum, top 10, monthly | +25.0% | 1.13 | -41.6% | +29.2% | 1.20 | -25.6% | +78.8% | +211.9% | 3/5 | no | **PASS** |
| 12-1 momentum, top 10, cash when Nifty < 200-day avg | +20.7% | 1.14 | -22.5% | +26.5% | 1.34 | -22.3% | +44.9% | +178.0% | 3/5 | no | **PASS** |
| 12-1 momentum, top 10, inverse-volatility weights | +22.3% | 1.04 | -41.4% | +26.8% | 1.16 | -26.0% | +49.2% | +182.4% | 3/5 | no | **PASS** |
| Dual momentum: top 10 by 12-month return, only if positive | +28.6% | 1.28 | -38.1% | +33.8% | 1.35 | -24.8% | +141.9% | +275.1% | 3/5 | no | **PASS** |
| Low volatility: 15 calmest (252-day vol) | +16.3% | 1.18 | -25.3% | +9.8% | 0.89 | -20.2% | -107.8% | +25.3% | 0/5 | no | fail |
| Trend: every stock above its 200-day avg, max 10% each | +22.5% | 1.34 | -31.5% | +21.8% | 1.38 | -18.7% | -6.0% | +127.2% | 2/5 | no | fail |
| Momentum + low-vol rank blend, top 10 | +14.9% | 0.95 | -34.7% | +6.6% | 0.53 | -27.8% | -128.8% | +4.3% | 0/5 | no | fail |

Excess = difference in total return over the whole period (not per year).

### Verdict per candidate

- **12-1 momentum, top 10, monthly** (`momentum_12_1`): PASSES the bar: beat buy-and-hold by +78.8% total return out-of-sample (+29.2%/yr vs +22.4%/yr) and won 60% of 5 yearly folds. In-sample it also beat buy-and-hold (+25.0%/yr vs +24.1%/yr, 71% of in-sample folds). Risk-adjusted it was not better (Sharpe 1.20 vs 1.29). Turnover 3.4x a year; charges cost 15.3% of starting capital over the test period.
- **12-1 momentum, top 10, cash when Nifty < 200-day avg** (`momentum_12_1_regime`): PASSES the bar: beat buy-and-hold by +44.9% total return out-of-sample (+26.5%/yr vs +22.4%/yr) and won 60% of 5 yearly folds. In-sample it trailed buy-and-hold (+20.7%/yr vs +24.1%/yr), so an out-of-sample win is less convincing. Risk-adjusted it was better too (Sharpe 1.34 vs 1.29). Turnover 2.6x a year; charges cost 9.1% of starting capital over the test period.
- **12-1 momentum, top 10, inverse-volatility weights** (`momentum_12_1_invvol`): PASSES the bar: beat buy-and-hold by +49.2% total return out-of-sample (+26.8%/yr vs +22.4%/yr) and won 60% of 5 yearly folds. In-sample it trailed buy-and-hold (+22.3%/yr vs +24.1%/yr), so an out-of-sample win is less convincing. Risk-adjusted it was not better (Sharpe 1.16 vs 1.29). Turnover 3.8x a year; charges cost 16.4% of starting capital over the test period.
- **Dual momentum: top 10 by 12-month return, only if positive** (`dual_momentum`): PASSES the bar: beat buy-and-hold by +141.9% total return out-of-sample (+33.8%/yr vs +22.4%/yr) and won 60% of 5 yearly folds. In-sample it also beat buy-and-hold (+28.6%/yr vs +24.1%/yr, 86% of in-sample folds). Risk-adjusted it was better too (Sharpe 1.35 vs 1.29). Turnover 3.3x a year; charges cost 16.4% of starting capital over the test period.
- **Low volatility: 15 calmest (252-day vol)** (`low_volatility`): Fails: trailed buy-and-hold by 107.8% out-of-sample and won only 0% of 5 folds. In-sample it trailed buy-and-hold (+16.3%/yr vs +24.1%/yr), so an out-of-sample win is less convincing. Risk-adjusted it was not better (Sharpe 0.89 vs 1.29). Turnover 1.4x a year; charges cost 3.6% of starting capital over the test period.
- **Trend: every stock above its 200-day avg, max 10% each** (`trend_sma200`): Fails: trailed buy-and-hold by 6.0% out-of-sample and won only 40% of 5 folds. In-sample it trailed buy-and-hold (+22.5%/yr vs +24.1%/yr), so an out-of-sample win is less convincing. Risk-adjusted it was better too (Sharpe 1.38 vs 1.29). Turnover 2.0x a year; charges cost 7.5% of starting capital over the test period.
- **Momentum + low-vol rank blend, top 10** (`momentum_lowvol`): Fails: trailed buy-and-hold by 128.8% out-of-sample and won only 0% of 5 folds. In-sample it trailed buy-and-hold (+14.9%/yr vs +24.1%/yr), so an out-of-sample win is less convincing. Risk-adjusted it was not better (Sharpe 0.53 vs 1.29). Turnover 3.6x a year; charges cost 8.1% of starting capital over the test period.

### Out-of-sample folds (strategy vs buy-and-hold total return)

| Strategy | Jan 2022-Dec 2022 | Jan 2023-Dec 2023 | Jan 2024-Dec 2024 | Jan 2025-Dec 2025 | Jan 2026-Oct 2026 |
|---|---|---|---|---|---|
| 12-1 momentum, top 10, monthly | +38.0% vs +20.0% W | +76.8% vs +49.0% W | +48.6% vs +24.7% W | +8.2% vs +13.9% | -14.3% vs -3.4% |
| 12-1 momentum, top 10, cash when Nifty < 200-day avg | +19.1% vs +20.0% | +55.7% vs +49.0% W | +48.6% vs +24.7% W | +8.7% vs +13.9% | +1.4% vs -3.4% W |
| 12-1 momentum, top 10, inverse-volatility weights | +34.0% vs +20.0% W | +72.3% vs +49.0% W | +51.8% vs +24.7% W | +6.0% vs +13.9% | -18.2% vs -3.4% |
| Dual momentum: top 10 by 12-month return, only if positive | +43.5% vs +20.0% W | +81.7% vs +49.0% W | +55.9% vs +24.7% W | +11.0% vs +13.9% | -12.4% vs -3.4% |
| Low volatility: 15 calmest (252-day vol) | +9.6% vs +20.0% | +27.8% vs +49.0% | +11.1% vs +24.7% | +9.7% vs +13.9% | -9.5% vs -3.4% |
| Trend: every stock above its 200-day avg, max 10% each | +25.4% vs +20.0% W | +51.6% vs +49.0% W | +23.5% vs +24.7% | +11.0% vs +13.9% | -5.1% vs -3.4% |
| Momentum + low-vol rank blend, top 10 | -0.3% vs +20.0% | +37.6% vs +49.0% | +14.1% vs +24.7% | +2.9% vs +13.9% | -16.6% vs -3.4% |

![equity](rotation_equity_nifty100.png)

## nifty50 (50 stocks, today's constituents)

In-sample 2015-01-01 to 2021-12-31; out-of-sample 2022-01-01 to 2026-10-29.

| Benchmark | IS CAGR | IS Sharpe | IS max DD | OOS CAGR | OOS Sharpe | OOS max DD | OOS total |
|---|---|---|---|---|---|---|---|
| Equal-weight buy & hold (net of costs) | +21.1% | 1.17 | -39.8% | +17.8% | 1.16 | -17.3% | +120.4% |
| Nifty 50 index ^NSEI (price, no costs) | +11.1% | 0.70 | -38.4% | +5.9% | 0.49 | -16.5% | +31.9% |

| Strategy | IS CAGR | IS Sharpe | IS max DD | OOS CAGR | OOS Sharpe | OOS max DD | OOS excess vs B&H | OOS excess vs Nifty | OOS folds won | Overfit? | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 12-1 momentum, top 10, monthly | +23.8% | 1.13 | -41.3% | +19.1% | 1.02 | -20.8% | +12.1% | +100.6% | 2/5 | no | fail |
| 12-1 momentum, top 10, cash when Nifty < 200-day avg | +17.6% | 1.09 | -20.1% | +19.1% | 1.23 | -15.1% | +11.7% | +100.2% | 2/5 | no | fail |
| 12-1 momentum, top 10, inverse-volatility weights | +21.8% | 1.08 | -42.1% | +16.4% | 0.94 | -22.9% | -12.7% | +75.8% | 2/5 | no | fail |
| Dual momentum: top 10 by 12-month return, only if positive | +20.6% | 1.00 | -40.9% | +18.4% | 0.99 | -21.8% | +5.5% | +94.0% | 2/5 | no | fail |
| Low volatility: 15 calmest (252-day vol) | +13.7% | 0.97 | -27.9% | +7.5% | 0.69 | -18.6% | -78.4% | +10.1% | 0/5 | no | fail |
| Trend: every stock above its 200-day avg, max 10% each | +17.9% | 1.07 | -39.0% | +13.7% | 1.00 | -18.2% | -34.6% | +53.8% | 0/5 | no | fail |
| Momentum + low-vol rank blend, top 10 | +14.1% | 0.92 | -29.3% | +7.1% | 0.58 | -24.1% | -81.2% | +7.2% | 0/5 | no | fail |

Excess = difference in total return over the whole period (not per year).

### Verdict per candidate

- **12-1 momentum, top 10, monthly** (`momentum_12_1`): Fails: won only 40% of 5 folds. In-sample it also beat buy-and-hold (+23.8%/yr vs +21.1%/yr, 57% of in-sample folds). Risk-adjusted it was not better (Sharpe 1.02 vs 1.16). Turnover 2.8x a year; charges cost 8.7% of starting capital over the test period.
- **12-1 momentum, top 10, cash when Nifty < 200-day avg** (`momentum_12_1_regime`): Fails: won only 40% of 5 folds. In-sample it trailed buy-and-hold (+17.6%/yr vs +21.1%/yr), so an out-of-sample win is less convincing. Risk-adjusted it was better too (Sharpe 1.23 vs 1.16). Turnover 2.4x a year; charges cost 6.6% of starting capital over the test period.
- **12-1 momentum, top 10, inverse-volatility weights** (`momentum_12_1_invvol`): Fails: trailed buy-and-hold by 12.7% out-of-sample and won only 40% of 5 folds. In-sample it also beat buy-and-hold (+21.8%/yr vs +21.1%/yr, 57% of in-sample folds). Risk-adjusted it was not better (Sharpe 0.94 vs 1.16). Turnover 3.2x a year; charges cost 9.7% of starting capital over the test period.
- **Dual momentum: top 10 by 12-month return, only if positive** (`dual_momentum`): Fails: won only 40% of 5 folds. In-sample it trailed buy-and-hold (+20.6%/yr vs +21.1%/yr), so an out-of-sample win is less convincing. Risk-adjusted it was not better (Sharpe 0.99 vs 1.16). Turnover 2.7x a year; charges cost 8.1% of starting capital over the test period.
- **Low volatility: 15 calmest (252-day vol)** (`low_volatility`): Fails: trailed buy-and-hold by 78.4% out-of-sample and won only 0% of 5 folds. In-sample it trailed buy-and-hold (+13.7%/yr vs +21.1%/yr), so an out-of-sample win is less convincing. Risk-adjusted it was not better (Sharpe 0.69 vs 1.16). Turnover 1.2x a year; charges cost 2.9% of starting capital over the test period.
- **Trend: every stock above its 200-day avg, max 10% each** (`trend_sma200`): Fails: trailed buy-and-hold by 34.6% out-of-sample and won only 0% of 5 folds. In-sample it trailed buy-and-hold (+17.9%/yr vs +21.1%/yr), so an out-of-sample win is less convincing. Risk-adjusted it was not better (Sharpe 1.00 vs 1.16). Turnover 2.3x a year; charges cost 6.4% of starting capital over the test period.
- **Momentum + low-vol rank blend, top 10** (`momentum_lowvol`): Fails: trailed buy-and-hold by 81.2% out-of-sample and won only 0% of 5 folds. In-sample it trailed buy-and-hold (+14.1%/yr vs +21.1%/yr), so an out-of-sample win is less convincing. Risk-adjusted it was not better (Sharpe 0.58 vs 1.16). Turnover 3.1x a year; charges cost 6.8% of starting capital over the test period.

### Out-of-sample folds (strategy vs buy-and-hold total return)

| Strategy | Jan 2022-Dec 2022 | Jan 2023-Dec 2023 | Jan 2024-Dec 2024 | Jan 2025-Dec 2025 | Jan 2026-Oct 2026 |
|---|---|---|---|---|---|
| 12-1 momentum, top 10, monthly | +8.8% vs +9.9% | +48.3% vs +41.3% W | +47.9% vs +21.8% W | +6.6% vs +13.5% | -11.0% vs -6.0% |
| 12-1 momentum, top 10, cash when Nifty < 200-day avg | +4.1% vs +9.9% | +37.1% vs +41.3% | +47.9% vs +21.8% W | +7.8% vs +13.5% | -0.1% vs -6.0% W |
| 12-1 momentum, top 10, inverse-volatility weights | +7.0% vs +9.9% | +48.5% vs +41.3% W | +45.0% vs +21.8% W | +3.3% vs +13.5% | -14.4% vs -6.0% |
| Dual momentum: top 10 by 12-month return, only if positive | +1.6% vs +9.9% | +47.6% vs +41.3% W | +50.1% vs +21.8% W | +8.5% vs +13.5% | -9.4% vs -6.0% |
| Low volatility: 15 calmest (252-day vol) | +5.9% vs +9.9% | +29.5% vs +41.3% | +8.2% vs +21.8% | +10.1% vs +13.5% | -13.4% vs -6.0% |
| Trend: every stock above its 200-day avg, max 10% each | +8.6% vs +9.9% | +35.7% vs +41.3% | +20.2% vs +21.8% | +7.1% vs +13.5% | -6.9% vs -6.0% |
| Momentum + low-vol rank blend, top 10 | -5.9% vs +9.9% | +37.2% vs +41.3% | +20.7% vs +21.8% | +6.3% vs +13.5% | -17.5% vs -6.0% |

![equity](rotation_equity_nifty50.png)

## Caveats (read before trusting any PASS)

1. **Survivorship bias (large).** The universe is TODAY's index members applied to the past. Stocks join the Nifty 50/100 *because* they rose, so the past looks better than it was, and momentum strategies (which buy what rose) benefit the most. Rough size: nifty100: equal-weight buy-and-hold of today's members made +24.1%/yr in 2015-2021 vs +11.1%/yr for the Nifty 50 price index (gap +13.0%/yr); nifty50: equal-weight buy-and-hold of today's members made +21.1%/yr in 2015-2021 vs +11.1%/yr for the Nifty 50 price index (gap +10.0%/yr). Part of that gap is dividends (~1-1.5%/yr, in the adjusted stock prices but not in the price index) and equal-weight vs cap-weight, the rest is hindsight. Compare the nifty50 and nifty100 tables: the wider, more mid-cap nifty100 list contains more 'future winners' that were small in 2015-2021.
2. **Data quality.** yfinance split/dividend-adjusted closes; occasional bad ticks, missing days, and re-listed/merged companies (e.g. HDFC Bank after the HDFC merger, demergers such as Jio Financial) can distort a 12-month return. Stocks listed recently enter only after a year of history.
3. **Few folds.** The out-of-sample period gives only ~5 yearly folds; a 3/5 score is weak evidence. A single regime (the 2023-24 Indian mid/large-cap rally, which strongly favoured momentum) dominates the test period.
4. **Costs.** NSE delivery (itemised), about 0.34% per round trip on Rs 1 lakh; 0 brokerage (discount broker), 0.05% slippage per side, DP charge per sell. Real impact cost for a larger account, and STT changes, would raise them. Monthly rebalancing of top-10 momentum turns over ~3x a year, so costs matter.
5. **Timing.** Decisions at the month's last close, fills at the next open; ignores the auction, circuit limits and partial fills. Cash earns 0 (a liquid fund would earn ~6-7%/yr, which would help the strategies that sit in cash).
6. **A PASS is evidence, not proof.** Seven candidates were tested; with that many tries one can pass by luck. Paper-trade before trusting it.

## Recommendation

No candidate passed on every universe tested without an overfit flag. Recommendation: **none**; keep holding cash (or simply buy-and-hold an index fund).
