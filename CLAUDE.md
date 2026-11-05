# CLAUDE.md — ETH Options Market-Making Simulator

## What this project is

A research-grade simulation of an ETH options market maker on Deribit, built as a portfolio project for a junior quantitative trader application (crypto options market making).

The simulator fits a vol surface, quotes bids and asks around it, gets filled by **real historical Deribit trades**, delta-hedges with the ETH perpetual, enforces Greek limits, and attributes P&L.

**Research question:** which quoting and risk parameters make money after adverse selection, hedging costs and fees, and why?

Everything uses historical data only. There is no live recording, no dashboard and no order-book data. Keep it that way unless the human says otherwise.

## How to work in this repo

- **One phase per session.** The human will say which phase. Stay inside it.
- **Plan before code.** For any non-trivial task, first propose a short plan (files, functions, tests) and wait for approval.
- **Tests first for numbers.** For pricing, Greeks, surface fitting and P&L, write the tests before (or with) the implementation.
- **Small commits.** Commit after each working piece with a clear message.
- **Explain modelling choices.** When you make one, add an entry to `reports/decisions.md`: the choice, the alternative, and why.
- **Never hard-code parameters.** Spreads, limits, windows, latency, fill fractions and the like live in `config/*.yaml`.
- **Ask rather than guess** when a choice changes results (fill rule, units, data filtering).
- Notebooks are for exploration only. No result may depend on a notebook.

## Commands

```
uv sync                                   # install
uv run pytest                             # all tests
uv run ruff check . && uv run ruff format .
uv run python -m mm.run --config config/base.yaml --start YYYY-MM-DD --end YYYY-MM-DD

# data pipeline (dates UTC, --end exclusive, default = config study_period)
uv run python -m mm.data.cli download --config config/base.yaml [--start D --end D]
uv run python -m mm.data.cli clean    --config config/base.yaml [--start D --end D]
uv run python -m mm.data.cli check    --config config/base.yaml
```

Python 3.12. Core libraries: numpy, scipy, pandas, pyarrow, duckdb, httpx, pydantic (config), matplotlib, pytest, ruff.

## Repo layout

```
config/base.yaml     # all parameters
data/                # git-ignored: raw/ (never modified), clean/
src/mm/
  data/              # downloaders, cleaning, instrument parsing
  pricing/           # Black-76, Greeks, forwards, IV solver
  surface/           # SVI fit, arbitrage checks; Surface A (marks), Surface B (trades)
  quoting/           # fair value, spread, skew, inventory adjustment
  sim/               # event-driven loop, fill model, fee model
  hedging/           # delta-hedge policies
  risk/              # Greek aggregation, limits, kill switch
  pnl/               # P&L attribution, markouts
  run.py             # single entry point
tests/               # one test file per module
notebooks/           # exploration only
reports/             # figures, decisions.md, write-up
```

## Data

All from Deribit's free public API. **Confirm each endpoint against the current docs before building on it.**

- **Historical option trades:** Deribit history server (`https://history.deribit.com/api/v2/public/...`). The main API only returns the last 24 hours of trades. Each trade carries: timestamp, instrument, price (ETH), amount, aggressor direction, trade IV, mark price, index price.
- **Perpetual trades and prices:** same history server. Gives the tick-level underlying for latency tests and hedging.
- **Futures prices:** for forwards per expiry.
- **DVOL:** `public/get_volatility_index_data`. Used for regime labels and as a sanity check.
- **Fees and contract specs:** from Deribit's published pages, stored in config.

Rules:
- Cache every raw download as Parquet under `data/raw/`, partitioned by date. Never overwrite raw files; cleaning writes to `data/clean/`.
- Query with DuckDB.
- Handle paging, rate limits and retries in downloaders.
- **Exclude block trades** (they have a block trade ID). They were negotiated off-book and never hit a quote.

## Units and conventions (strict)

Most bugs in this project will be silent unit errors. Follow these everywhere.

- **Time:** all timestamps UTC. Raw: integer milliseconds. Clean: timezone-aware UTC datetimes.
- **Time to expiry:** years, ACT/365, computed to the expiry timestamp (Deribit expiries are 08:00 UTC).
- **Vol:** decimal (0.62, not 62). Vega is reported per 1 vol point (0.01).
- **Prices:** suffix columns with the currency, e.g. `price_eth`, `price_usd`. Option premiums on Deribit are in ETH. Strikes are in USD.
- **Size:** ETH option contracts (1 contract = 1 ETH underlying; verify in specs).
- **Rates:** price with Black-76 on the forward from the matching future. No separate interest-rate curve.
- **Coin-margined options:** ETH price ≈ USD Black-76 price / underlying price. Delta in coin terms needs a premium adjustment. **Verify both against Deribit's mark prices and displayed Greeks** before trusting them.
- **P&L:** reported in USD, converted at the index price at each timestamp.

## Key modelling decisions (already agreed)

1. **Two speeds.**
   - Option *prices* are re-priced at every historical trade from the latest surface and the underlying price at that moment.
   - The *vol surface* is refitted every 1–5 minutes (config).
   - Hedging and risk checks run on their own schedule (config).
2. **Latency parameter.** The underlying price used for re-pricing is taken from `latency` before the trade (sweep 0 ms, 100 ms, 1 s, 5 s). Never assume zero latency without stating it.
3. **Fill model (conservative).**
   - A taker sell at price p fills our bid only if our bid is **strictly above** p. A taker buy fills our ask only if our ask is **strictly below** p.
   - If exactly at p: fill with probability or fraction `at_price_fill_fraction` (config).
   - Fill size = min(trade size, our quote size) × `max_fill_fraction` (default 0.5).
   - Every fill is logged with its reason.
4. **Two surfaces, same SVI code.**
   - **Surface A (main):** SVI per expiry fitted to mark IVs (from mark prices attached to trades) in a trailing window, vega-weighted.
   - **Surface B (experiment, Phase 9):** fitted to actual trade IVs, with extra smoothing for sparse wings and long expiries.
   - Interpolate across expiries in total variance. Check butterfly and calendar arbitrage.
5. **Quoting** is in vol space: fair vol ± spread, with spread widening for short expiry, wings and high realised vol, and an inventory skew that leans quotes back towards flat vega and delta.
6. **Validation benchmark:** Deribit's mark prices. There is no historical bid/ask, so spread width is tested across a range, not against the real market.

## Non-negotiables

- **No lookahead.** Any value used at time t must come from data timestamped before t (after latency). Add tests that fail if a later timestamp is used.
- **Time-split experiments.** Tune parameters on the first part of the period; report results on the later part. Report calm and stress periods separately.
- **P&L must reconcile.** Attribution components (spread capture, delta, gamma, theta, vega, hedging cost, fees, residual) must sum to total P&L; test this.
- **Reproducibility.** Every reported number must come from `mm.run` with a committed config.

## Phases

Work through these in order. A phase is finished only when every "done when" check passes. Don't start the next phase until then.

### Phase 0: Setup (half a day)
- Repo, `uv` environment, ruff, pytest, this file, `config/base.yaml` with placeholder parameters, `reports/decisions.md`.
- **Done when:** `uv run pytest` passes and `python -m mm.run` executes a hello-world.

### Phase 1: Data pipeline (2–3 days)
- Downloaders for historical ETH option trades, perpetual trades and prices, futures prices, and DVOL, with paging, retries and rate-limit handling. Cache all raw downloads.
- Remove block trades.
- Instrument parser: `ETH-27DEC26-4000-C` → underlying, expiry, strike, type.
- Cleaning: UTC timestamps, drop obvious bad prints, time to expiry in years.
- **Done when:** any day's trades load in one line, and a plot of daily trade counts and DVOL shows no gaps that can't be explained.

### Phase 2: Pricing (2 days)
- Black-76 on the forward, Greeks (delta, gamma, vega, theta), IV solver.
- Forward per expiry from the matching future, or put-call parity where none exists.
- Coin-margined pricing and premium-adjusted delta.
- **Done when:** put-call parity and IV round-trip tests pass, and the model reproduces the mark prices attached to historical trades to within a few basis points of vol.

### Phase 3: Surface A, fitted to Deribit marks (2 days)
- Convert mark prices into mark IVs.
- At each refit time, fit SVI per expiry to mark IVs from a trailing window (e.g. 30–60 min), vega-weighted.
- Butterfly and calendar arbitrage checks; interpolate across expiries in total variance.
- Time series of ATM vol, 25-delta risk reversal, 25-delta butterfly.
- **Done when:** fit error against marks is small, no arbitrage is flagged on normal days, and ATM vol tracks DVOL.

### Phase 4: Simulator and fill model (3–4 days)
- Event-driven loop over historical trades: at each trade, re-price quotes from the latest surface and the latency-shifted underlying price, then apply the fill model. Surface refits, hedging and risk checks run on their own schedules.
- Fee model from Deribit's schedule.
- Start with a fixed-spread quoter.
- **Done when:** the fixed-spread quoter runs end to end on one week of data, every fill is logged with its reason, and a hand-checked sample of 20 fills is correct.

### Phase 5: Quoting engine (2 days)
- Fair vol from the surface, bid/ask = fair ± spread (vol points).
- Spread widens for short expiry, wing strikes and high realised vol.
- Inventory skew: shift both quotes against current vega and delta.
- Size and per-strike position limits.
- **Done when:** each parameter has a test showing it moves quotes in the right direction.

### Phase 6: Hedging and risk (2–3 days)
- Delta-hedge with the perpetual. Policies to compare: fixed interval, delta band, gamma-scaled band.
- Greeks aggregated by expiry and in total; vega and gamma limits stop quoting on the side that would increase risk.
- Kill switch on sudden moves (large 1-minute return or DVOL jump).
- **Done when:** net delta stays inside its band through a stress period, and limit breaches visibly cut quoting.

### Phase 7: P&L attribution (2 days)
- Split P&L into spread capture (edge vs fair at fill), delta, gamma, theta, vega, hedging cost, fees, residual.
- Markouts: change in fair value 1 min, 5 min and 1 h after each fill.
- **Done when:** components sum to total P&L with a small residual, and the worst day can be explained from the breakdown.

### Phase 8: Experiments (3–4 days)
- Baseline: fixed spread, no inventory skew, hourly hedging.
- Sweeps: spread width, inventory-skew strength, hedge policy, vol-dependent widening, latency (0 ms, 100 ms, 1 s, 5 s).
- Tune on the first part of the period, report on the later part; calm and stress periods separately.
- **Done when:** there is a results table (P&L, Sharpe, max drawdown, fill count, average markout per variant) and a clear sentence on what worked and why.

### Phase 9: Surface B, fitted to trade IVs (2–3 days)
- Same SVI code on actual trade IVs, with smoothing and minimum-data rules for sparse wings and long expiries.
- Compare with Surface A: where and how much they differ (wings, short expiries, stress periods).
- Rerun the best Phase 8 quoter on Surface B; compare P&L, fill count and markouts.
- **Done when:** one sentence can say whether quoting off our own surface helped or hurt, and why.

### Phase 10: Write-up (2 days)
- 3–5 page research note plus README: question, setup and simplifications, results table, 2–3 charts (P&L attribution over time, markouts by expiry and moneyness, worst stress period), next steps.
- **Done when:** every number in the note can be reproduced from one `mm.run` command.
