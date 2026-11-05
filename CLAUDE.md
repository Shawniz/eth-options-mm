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
```

Python 3.13 (newest Homebrew; see decisions D001). Core libraries: numpy, scipy, pandas, pyarrow, duckdb, httpx, pydantic (config), matplotlib, pytest, ruff.

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

## Phases (see the project plan for full detail)

0. Setup: repo, env, tests, this file
1. Data pipeline: downloaders, block-trade filter, instrument parser, cleaning
2. Pricing: Black-76, Greeks, IV solver, forwards; reproduce Deribit marks
3. Surface A: SVI on mark IVs, arbitrage checks, ATM / 25d RR / 25d fly time series
4. Simulator: event-driven loop, fill model, fee model; hand-check 20 fills
5. Quoting engine: spread, skew, inventory adjustment, size limits
6. Hedging and risk: delta-hedge policies, vega/gamma limits, kill switch
7. P&L attribution and markouts (1 min, 5 min, 1 h)
8. Experiments: baseline vs variants, latency sweep, calm vs stress, time split
9. Surface B: trade-IV surface, compare with A, rerun best quoter
10. Write-up: research note and README

Each phase has "done when" checks in the plan. Don't move on until they pass.
