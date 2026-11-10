# CLAUDE.md — ETH Options Market-Making Simulator

## What this project is

A research-grade simulation of an ETH options market maker on Deribit, built as a portfolio project for a junior quantitative trader application (crypto options market making).

The simulator fits a vol surface to Deribit mark prices, quotes bids and asks around it, gets filled by **real historical Deribit trades**, delta-hedges with the ETH perpetual, enforces risk limits, and attributes P&L.

**Research question:** which quoting and risk choices make money after adverse selection, hedging costs and fees, and why?

Scope is deliberately small: historical data only, no live recording, no dashboard, no order-book data. Keep it that way unless the human says otherwise. Ideas outside scope go in `reports/extensions.md`, not into the code.

## How to work in this repo

- **One phase per session.** The human will say which phase. Stay inside it.
- **Plan before code.** For any non-trivial task, propose a short plan (files, functions, tests) and wait for approval.
- **Tests first for numbers.** Pricing, Greeks, surface fitting, accounting and P&L get tests before or with the implementation.
- **Small commits** after each working piece, with clear messages.
- **Log modelling choices** in `reports/decisions.md`: the choice, the alternative, why, and the parameter's category (from data / from theory / free choice).
- **No hard-coded parameters.** Everything tunable lives in `config/*.yaml`.
- **Ask rather than guess** when a choice changes results (fill rule, units, data filtering).
- Notebooks are for exploration only. No result may depend on a notebook.

## Commands

```
uv sync                                   # install
uv run pytest                             # all tests
uv run ruff check . && uv run ruff format .
uv run python -m mm.run --config config/base.yaml --start YYYY-MM-DD --end YYYY-MM-DD
uv run python -m mm.sim.handcheck --config config/base.yaml --run START_END   # 20 fills recomputed
uv run python -m mm.pnl.report --config config/base.yaml --run START_END      # P&L attribution, markouts
uv run python -m mm.pnl.smile_dynamics --config config/base.yaml              # sticky strike vs moneyness

# data pipeline (UTC dates, --end exclusive, default = config study_period)
uv run python -m mm.data.cli download --config config/base.yaml [--start D --end D]
uv run python -m mm.data.cli clean    --config config/base.yaml [--start D --end D]
uv run python -m mm.data.cli check    --config config/base.yaml
```

Python 3.13 (owner's newest Homebrew Python; decision D001). Libraries: numpy, scipy, pandas, pyarrow, duckdb, httpx, pydantic (config), matplotlib, pytest, hypothesis, ruff.

## Repo layout

```
config/base.yaml     # all parameters
data/                # git-ignored: raw/ (never modified), clean/, cache/
src/mm/
  data/              # downloaders, cleaning, instrument parsing
  pricing/           # Black-76, coin-settled conversion, Greeks, IV solver
  surface/           # SVI fit, arbitrage checks, summary statistics
  quoting/           # fair value, spread, inventory skew, sizing
  sim/               # event loop, fill model, fees, settlement, funding
  hedging/           # delta-band hedger, inverse-perpetual conversion
  risk/              # Greek aggregation, limits
  pnl/               # waterfall attribution, markouts
  experiments/       # experiment runner and result tables
  run.py             # single entry point
tests/
notebooks/           # exploration only
reports/             # figures, decisions.md, extensions.md, write-up
```

## Data

All from Deribit's free public API. **Confirm each endpoint and field against the current docs before building on it.**

- **Historical option trades:** Deribit history server (`https://history.deribit.com/api/v2/public/...`); the main API only keeps the last 24 hours. Each trade carries timestamp, instrument, price (ETH), amount, aggressor direction, trade IV, mark price, index price, and a sequence number.
- **Perpetual trades:** same history server. The tick-level underlying, used for latency and hedge execution.
- **Futures prices:** forwards per expiry.
- **Funding rates:** historical perpetual funding.
- **Delivery prices:** official expiry settlement prices (e.g. `public/get_delivery_prices`; verify).
- **DVOL:** `public/get_volatility_index_data`, for sanity checks and calm/stress labels.
- **Fees and contract specs:** option and perpetual fees, tick sizes, minimum order sizes, contract multipliers, the inverse perpetual's contract size. Stored in config.

Rules:
- Cache every raw download as Parquet under `data/raw/`, partitioned by date. Never overwrite raw files; cleaning writes to `data/clean/`. Query with DuckDB.
- Handle paging, rate limits and retries.
- **Exclude block trades** (negotiated off-book).
- **Exclude combo/strategy legs** (leg prices can be off-market). Check how they are flagged; log how many were removed.
- Keep liquidation trades, but tag them.
- Make sure expired instruments are included in downloads, not just currently listed ones.

## Units and conventions (strict)

Most bugs here will be silent unit errors.

- **Time:** UTC everywhere. Raw: integer milliseconds. Clean: timezone-aware UTC datetimes.
- **Time to expiry:** years, ACT/365, to the expiry timestamp (Deribit expiries are 08:00 UTC; verify).
- **Vol:** decimal (0.62). Vega reported per 1 vol point (0.01), in USD.
- **Price columns** carry their currency: `price_eth`, `price_usd`. Option premiums on Deribit are in ETH; strikes in USD.
- **Size:** option contracts in ETH (verify contract multiplier in specs).
- **Base currency for P&L and risk: USD.**

## Pricing (agreed maths)

- Price off the **forward** F for each expiry using **Black-76, undiscounted**. F = basis × latest index, where basis = mark/index of the matching dated future; for expiries without a future, ln(basis) is interpolated linearly in T between the futures (put-call parity was tested and rejected; decision D017):
  `C = F·N(d1) − K·N(d2)`, `d1 = [ln(F/K) + ½σ²T] / (σ√T)`, `d2 = d1 − σ√T`.
- **ETH premium = C / F.** (USD discounting is absorbed into F; ETH discounting ≈ 0 on Deribit.) Verify against Deribit mark prices.
- Black delta `N(d1)` is the per-option hedge delta. Deribit marks move close to sticky strike over the hours a hedge is held (D040), where Black delta is right (D041). The sticky-moneyness delta `N(d1) − φ(d1)·√T·∂σ/∂k` (D038) is kept as `hedging.delta: smile`. Do **not** apply an F/S factor for Deribit's coin-settled options.
- Use Black delta (not premium-adjusted) to locate 25-delta strikes.
- In code, convert price amounts to vol by exact re-pricing (solve P(σ+δσ) = P(σ) + δP), not the vega approximation.

## Account delta and hedging (agreed maths)

The account holds: ETH balance `B` (collateral, premiums, fees, funding, settled P&L), options `n_i`, and an inverse perpetual with USD notional `Q` entered at `P0`.

- USD value: `W = S·B + Σ n_i·(C_i/F_i)·S + Q·(1/P0 − 1/P)·S`
- **Account delta (USD per $1 move in ETH):** `Δ_USD = B + Σ n_i·N(d1_i) + Q/P0` (D041)
- **Hedge trade:** `ΔQ = −S·Δ_USD` (USD notional), converted to perpetual contracts using the contract size from specs, rounded to whole contracts.
- The premium adjustment happens automatically because premiums flow through `B`. Never also subtract premium from option deltas (no double counting).
- Unit test: when perpetual P&L is settled into `B` and `P0` resets, total delta is unchanged.

## Key modelling decisions

1. **Event-driven simulation in chronological order.** Merge option trades, perpetual trades and funding times by timestamp; keep each stream's exchange sequence order. Self-generated events (surface refits, hedge checks) use a fixed tie-break order: underlying update → surface refit → quote and fill → hedge. Runs must be deterministic (seed any randomness from config).
2. **Two speeds.** Option quotes are re-priced at every trade from the latest surface and the latency-shifted underlying. The surface is refitted every 1–5 minutes (config).
3. **Latency parameter.** Quotes use only information timestamped before `trade_time − latency`.
4. **Fill model (strict).**
   - Taker sell at price p fills our bid only if `bid > p`; taker buy fills our ask only if `ask < p`.
   - Exactly at p: fill fraction `at_price_fill_fraction` (config, default 0).
   - Fill size = `min(trade size, our quote size) × max_fill_fraction` (default 0.5).
   - Fill price = our quote. Option fills pay **maker** fees.
   - Every fill is logged with its reason.
5. **Quoting universe** (config): expiries from `min_days` to `max_days` (`min_days` is also the near-expiry cut-off), strikes with OTM delta min(|Δ_call|, |Δ_put|) ≥ `min_abs_delta` at fair vol (D030).
6. **Spread:** a flat half-spread in vol points around fair vol, with a minimum price width (ticks, and enough to cover fees). Bids rounded down, asks up.
7. **Inventory skew:** both quotes shift by `Δσ = −κ · net_vega / vega_limit` (total book vega; the vega limit is the scale, D035), capped at ±h so we never quote through fair (D032).
8. **Sizing:** capped vega-normalised by default: `size = min(target_vega / vega_i, max_contracts)`, rounded down to the order increment; no quote below the minimum order size. Fixed-contract sizing kept as a config option for a robustness check.
9. **Hedging:** one policy: when `|Δ_USD|` exceeds `delta_band` (checked after every perp print and option fill), hedge back to zero with the perpetual as a **taker**. The order fills at the first same-side perp print strictly after the decision, at or after decision + latency, at that print ± `hedge_cost_bps`, plus the taker fee (D036). The collateral is hedged too (D034).
10. **Limits:** total vega limit and per-instrument position limit. Each side's size is cut to the room left; a side below the minimum order is dropped, so near a limit only the risk-reducing side is quoted (D035).
11. **Funding** accrues on the perpetual position at Deribit's funding times.
12. **Expiry settlement** at Deribit's official delivery price; positions are removed and settled in ETH.
13. **Valuation for P&L:** always at Surface A (the market's fair value), never at a quoting variant's own surface.

## P&L attribution

- Snapshot every minute, and also revalue at every fill and hedge event.
- **Valuation** uses information up to and including t (it is measurement; decisions keep the latency rule; D039).
- **Waterfall revaluation**, in the fixed order time → spot → basis → vol (D039):
  - theta = V(t1,S0,b0,σ0) − V(t0,S0,b0,σ0)
  - spot = V(t1,S1,b0,σ0) − V(t1,S0,b0,σ0), split into delta = Δ_USD·dS and gamma = the remainder
  - basis = V(t1,S1,b1,σ0) − V(t1,S1,b0,σ0) (futures basis and perp premium)
  - vega = V(t1,S1,b1,σ1) − V(t1,S1,b1,σ0)
- **Flows:** spread capture (side × (fair − fill price) at each option fill), hedge execution cost, fees, funding, settlement difference.
- **Total P&L = theta + delta + gamma + basis + vega + spread capture + hedge cost + fees + funding + settlement + residual.** The residual must be ≈ 0; a large residual is a bug or a missing flow.
- Greeks for attribution are computed by bump-and-revalue of the full USD account.
- **Markouts** per fill at 1 min, 5 min, 1 h: `side × (fair(t+h) − fill price)`, raw and **delta-hedged** (subtract Δ·(S(t+h) − S(t))). Delta-hedged is the headline.

## Non-negotiables

- **No lookahead.** Every input at time t must be timestamped before t (after latency). Add tests that fail otherwise.
- **Time split.** Calibrate and choose parameters on the first ~65% of the period; report on the rest. Thresholds (including the calm/stress DVOL threshold) come from the training period only.
- **Accounting invariants** asserted after every event: the ETH balance changes only through premiums, fees, funding, settlement and perpetual P&L; positions change only through fills, hedges and expiry.
- **P&L reconciles** (components sum to total).
- **Reproducible:** every reported number comes from `mm.run` with a committed config.
- **Cache strategy-independent work** (cleaned data, forwards, surface fits, fair values) so experiment runs only redo quoting, fills, hedging and accounting.

## Phases

Work in order. A phase is finished only when every "done when" check passes.

### Phase 0: Setup (half a day)
- Repo, `uv` environment, ruff, pytest, this file, `config/base.yaml` with placeholders, `reports/decisions.md`, `reports/extensions.md`.
- **Done when:** `uv run pytest` passes and `python -m mm.run` runs a hello-world.

### Phase 1: Data pipeline (2–3 days)
- Downloaders for option trades, perpetual trades, futures prices, funding rates, delivery prices, DVOL, with paging, retries, rate limits and caching.
- Filters: block trades, combo legs; tag liquidations.
- Instrument parser: `ETH-27DEC26-4000-C` → underlying, expiry, strike, type.
- Cleaning: UTC timestamps, bad prints, time to expiry.
- Record contract specs and fees in config.
- **Done when:** any day's data loads in one line, filter counts are logged, and a plot of daily trade counts and DVOL shows no unexplained gaps.

### Phase 2: Pricing (2 days)
- Black-76 (undiscounted), ETH premium C/F, Greeks, IV solver, forwards per expiry.
- **Done when:** put-call parity and IV round-trip tests pass, the Deribit ticker fixture passes, and on historical trades (inclusive timing, D018) our IV vs Deribit's trade IV has median ≤ 0.05 and p95 ≤ 0.25 vol points for expiries > 4 days, and ≥ 95% of all trades are reproduced within ½ tick of price (D019). `uv run python -m mm.pricing.validate --config config/base.yaml`.

### Phase 3: Surface A (2 days)
- Mark prices → mark IVs. At each refit, fit an eSSVI slice (θ, ρ, ψ; butterfly-free by construction, calendar-constrained against the shorter expiry; D023) per expiry to mark IVs from a trailing window (60 min, D020), pooling in log-moneyness k = ln(K/F) at each observation's own time, vega- and recency-weighted, warm-started from the previous fit, with minimum-data rules (full → carry → none; D021, D026).
- Butterfly (Gatheral g(k) ≥ 0) and calendar (total variance non-decreasing in T) checks on a k grid; interpolate across expiries linearly in total variance.
- Time series of ATM vol, 25-delta risk reversal and 25-delta butterfly (25-delta strikes found by root-finding with the smile's own vol).
- **Done when:** fit error against marks meets the per-expiry-bucket targets, ≥ 95% of refits on normal days have no arbitrage flag, and 30-day ATM vol tracks DVOL (daily-change correlation ≥ 0.6, DVOL − ATM within [0, 8] vol points in ≥ 95% of hours) (D026). `uv run python -m mm.surface.report --config config/base.yaml`.

### Phase 4: Simulator (3 days)
- Event loop, strict fill model, maker fees, funding accrual, expiry settlement, accounting invariants, fill log.
- Start with a fixed-spread quoter.
- **Done when:** it runs end to end on one week, every fill has a logged reason, a synthetic unit test with known outcomes passes, 20 hand-checked fills are correct, and our share of traded volume is plausible.

### Phase 5: Quoting (2 days)
- Fair vol from the surface, flat vol half-spread with price floor and correct rounding, inventory skew on total vega, capped vega-normalised sizing, quoting universe and near-expiry cut-off.
- **Done when:** tests show each parameter moves quotes in the right direction, and a property-based test confirms bid < ask, non-negative prices and correct rounding for random inputs.

### Phase 6: Hedging and risk (2 days)
- Account delta (formula above), inverse-perpetual conversion, delta-band hedger (taker, back to zero), total vega and per-instrument limits with one-sided quoting.
- **Done when:** unit tests pass for hedge sign, inverse-perpetual conversion, funding sign and delta invariance at perpetual settlement; account delta stays inside the band through a stress period; limit breaches visibly cut quoting.

### Phase 7: P&L attribution (2 days)
- Waterfall attribution, flows, residual, delta-hedged and raw markouts.
- **Done when:** synthetic scenarios give the expected components (pure spot move → gamma ≈ ½ΓdS²; pure time → theta; vol shift → vega; one fill with no market move → spread capture = edge = markout), components sum to total P&L with a near-zero residual on real data, and the worst day can be explained line by line.

### Phase 8: Experiments (3 days)
- Baseline: the Phase 5 quoter with default parameters.
- Three experiments, one parameter at a time, on the test period:
  1. **Spread level:** e.g. 0.5, 1, 2, 3 vol points.
  2. **Inventory skew:** off vs two strengths of κ.
  3. **Latency:** 0 ms, 1 s, 5 s.
- Robustness: the best setting with a looser and a stricter fill model, and with fixed-contract sizing.
- Metrics: P&L by component, daily Sharpe (365-day annualisation), max drawdown, fill count and volume share, delta-hedged markouts, vega usage. Calm and stress reported separately.
- Compare each variant with the baseline by **paired daily P&L differences**: mean ± 2 standard errors.
- **Done when:** one results table and a sentence per experiment explaining the mechanism behind the result.

### Phase 9: Write-up (2 days)
- 3–5 page research note: question, setup, simplifications, results table, three charts (cumulative P&L stacked by component; markout vs horizon by expiry and delta bucket; worst day), what was learned, next steps from `reports/extensions.md`.
- README: what, how to run (one command), layout, where results are.
- **Done when:** every number reproduces from one `mm.run` command.

## Extensions (do not build unless asked)

Surface B (trade-IV surface) compared with Surface A; vega-based calibrated spread form and regime widening; vega buckets by tenor; kill switch; hedge-policy comparison (fixed interval, Whalley–Wilmott band); dated-future hedging for long expiries; ETH-denominated book; Aave liquidation-pressure signal.
