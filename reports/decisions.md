# Modelling and engineering decisions

Each entry: the choice, the alternative, why, and the parameter's category:
**from data** (measured or read from Deribit), **from theory** (follows from the maths or market structure), or **free choice** (a judgement call, a candidate for robustness checks).

---

## D001 — Python 3.13 instead of 3.12
- **Category:** free choice
- **Choice:** Python 3.13 (newest Homebrew install, 3.13.5).
- **Alternative:** 3.12, as first written in CLAUDE.md.
- **Why:** The owner asked for the newest local Python. All core libraries support 3.13.

## D002 — Strict config validation
- **Category:** free choice (engineering)
- **Choice:** Pydantic models with `extra="forbid"`. Out-of-range values fail at load.
- **Alternative:** Plain dicts read from YAML.
- **Why:** A misspelt parameter would otherwise fall back to a default without anyone noticing and change results.

## D003 — `at_price_fill_fraction` = 0.0
- **Category:** from theory (queue priority), with a robustness check
- **Choice:** Trades exactly at our quote price do not fill us. All main runs and all experiments use 0.0.
- **Alternative:** A partial fill, for example 0.25 to 0.5, or a full sweep.
- **Why:** Price-time priority. A quote strictly better than p would have been hit first, but at p we tie with resting orders whose queue position is unknown without order-book data. Small trades usually only fill the front of the queue, and the ties that do reach deep into the queue tend to be the most adverse.
- **Robustness (owner decision, 2026-11-08):** There is no sweep. Only the **winning Phase 8 configuration** is rerun once under a looser and once under a stricter fill model, as CLAUDE.md Phase 8 requires.

## D004 — Study period 2025-01-01 to 2025-06-30
- **Category:** free choice
- **Choice:** Six months of UTC days, start inclusive and end (2025-07-01) exclusive. Set in `config/base.yaml` under `study_period`.
- **Alternative:** A shorter window, or a whole year.
- **Why:** It covers calm stretches and stress periods (the early-February crash and the April tariff selloff), which leaves room for a time split. It's about 0.5 GB on disk, measured.

## D005 — Exclude combo trades as well as block trades
- **Category:** from theory (market structure)
- **Choice:** Drop trades with `combo_id` (and with `block_trade_id`). Each is controlled by a flag in `data.exclude_*`.
- **Alternative:** Keep combo legs as fills.
- **Why:** Combo legs match on the combo book at prices allocated to each leg. They are not hits on a single-leg quote, so counting them as fills would inflate our fill rate. Owner agreed.

## D006 — Keep liquidation trades
- **Category:** from theory (market structure)
- **Choice:** Keep trades with a `liquidation` flag and mark them `is_liquidation`.
- **Alternative:** Drop them.
- **Why:** They execute against the order book, so they could have hit our quote. They are also realistic adverse flow. The flag allows analysing them separately.

## D007 — Raw storage is lossless; one file per UTC day
- **Category:** free choice (engineering)
- **Choice:** `data/raw/<source>/date=YYYY-MM-DD/part.parquet`. Known fields are typed columns, and any field we don't recognise goes into `extra_json`. Each day is written once, atomically, and never overwritten. Days that aren't finished are refused. Instrument specs are snapshots per fetch date.
- **Alternative:** Store raw JSON, or append to a single file.
- **Why:** Raw stays an exact record of what the API returned. Re-runs are cheap because cached days are skipped.

## D008 — Paging and inclusive bounds
- **Category:** from data (API behaviour, verified)
- **Choice:** Each day is requested as `[00:00:00.000, 23:59:59.999]` (both bounds were checked to be inclusive). Pages advance from the last timestamp seen, trades are de-duplicated by `trade_id`, and the download fails loudly if a full page shares one millisecond.
- **Alternative:** Skip past the boundary millisecond. That would silently drop trades.
- **Why:** The API pages by timestamp only, so trades at the boundary come back twice.

## D009 — DVOL is hourly and carries `available_at`
- **Category:** from data (API availability)
- **Choice:** Hourly DVOL candles from the main API. `available_at = ts + 1h` is the time the candle's close becomes known.
- **Alternative:** 1-minute candles. These are not available historically (the history server has no DVOL at all).
- **Why:** Hourly is enough for regime labels. `available_at` prevents lookahead: candles are stamped at their open, but the close is only known an hour later.

## D010 — Futures and perp downloaded together, split when cleaning
- **Category:** from data (contract specs)
- **Choice:** A single `kind=future` download, which includes ETH-PERPETUAL, split into `perp_trades` and `future_trades` during cleaning. Sizes are stored as `amount_usd`, because these are reversed contracts with USD notional.
- **Alternative:** A separate per-instrument download for the perp.
- **Why:** It's one paging loop and the result is identical. Note that there are no daily futures: forwards for daily option expiries will need interpolation (Phase 2).

## D011 — Contract specs and fees from the study period, checked against Deribit
- **Category:** from data
- **Choice:** `config/base.yaml → specs`.
  - Options: 1 ETH contract, minimum (and increment) 1 contract, tick 0.0001 ETH up to a price of 0.005 and 0.0005 above. The maker and taker fee is 0.0003 ETH per contract (0.03% of the underlying), capped at 12.5% of the option price.
  - Perp: an inverse contract with a $1 contract size, minimum $1, tick $0.05. Fees are maker 0 and taker 0.05%.
  - `mm.data.cli check` fails if the config disagrees with Deribit's records for any instrument alive during the study period.
- **Alternative:** Read fees from today's API.
- **Why:** Fees change over time. Today's perp record shows maker 1.5 bp and taker 3.5 bp, a schedule from August 2026, but in H1 2025 the perp was 0 / 5 bp. Option fees and ticks are identical on all 12,508 options expiring in the study period. The dated futures alive in the period carry taker 0.05%, which confirms the perp's H1 2025 taker fee. The 12.5% cap comes from secondary sources (the official fee page was blocked). The instrument data confirms the 0.0003 rate.

## D012 — Option delivery fee
- **Category:** from data
- **Choice:** 0.00015 ETH per contract (0.015% of the underlying), capped at 12.5% of the option value. Daily options are treated as exempt.
- **Alternative:** Zero.
- **Why:** The owner confirmed the rate and cap on 2026-11-08. **The daily-option exemption is still unverified**; it's a config flag (`delivery_fee_exempt_daily`), so it can be switched off if wrong. It only matters from Phase 4, for positions held to expiry.

## D013 — Bad prints: drop invalid, flag suspect
- **Category:** free choice (thresholds); from theory (the invalid rules)
- **Choice:**
  - **Drop** rows that cannot be real: price ≤ 0, size ≤ 0, missing mark or index, or an option traded at or after expiry. They are counted as `invalid`.
  - **Keep and flag** suspect option prints (`is_suspect`, `suspect_reason`):
    - `iv_nonpositive`: trade iv ≤ 0
    - `iv_high`: trade iv > `data.suspect.iv_max` (3.0)
    - `far_from_mark`: |price − mark| > 50% of mark **and** more than 5 minimum ticks away
- **Alternative:** Drop suspect prints in cleaning.
- **Why:** Suspect prints are often real: deep in-the-money options, or options at the minimum tick near expiry, where the IV is meaningless but a quote could still be hit. Phase 2/3 decide what feeds the surface; the fill model still sees them. The owner chose this.

## D014 — Funding: hourly records, start bound exclusive
- **Category:** from data (API behaviour, verified)
- **Choice:** `get_funding_rate_history` is downloaded one UTC day per request, starting from `start − 1 ms`. Each record (`interest_1h`, `interest_8h`, both fractions) covers the hour **ending** at its timestamp, so `available_at = ts`.
- **Alternative:** Request long ranges.
- **Why:** The endpoint silently keeps only the latest 744 records of a longer range, and its `start_timestamp` is exclusive, unlike the trade endpoints. The first download missed the 00:00 record each day; those files were moved to `data/raw/_quarantine/`, not deleted. How funding accrues on the position (hourly vs continuous) is decided in Phase 4.

## D015 — Delivery prices: one snapshot, settled at 08:00 UTC
- **Category:** from data
- **Choice:** All daily `eth_usd` delivery prices are fetched in one snapshot, paged 100 at a time. `settle_ts` is the date at 08:00 UTC. `check` fails if any expired, traded option expiry has no delivery price.
- **Alternative:** Use the index price at 08:00.
- **Why:** Deribit settles options at its official delivery price (an average of the index before expiry), not at a single index print.

## D016 — Deribit fixture: tolerances and what they mean
- **Category:** from data
- **Choice:** A one-off snapshot of 151 live ETH tickers across all 12 expiries (9 hours to 319 days), committed as `tests/fixtures/deribit_tickers_2026-11-08.json`. It was made by `scripts/make_deribit_fixture.py` and approved by the owner.
  - Price passes if `|ours − mark| ≤ 1e-4 + |Δ − p| × 6 bp`, where Δ − p = ∂p_eth/∂ln F, and the median forward gap is ≤ 1 bp.
  - Greeks: delta within 5e-5, gamma within 1e-5, vega within 0.1%, theta within 0.1% (T ≥ 7 days only).
- **Alternatives:** A flat price tolerance, which fails deep ITM long-dated options for reasons unrelated to the formula. Or solving each option's implied forward, which is noise-dominated because marks are rounded to 1e-4.
- **Why:** With Deribit's own displayed forward and mark_iv, 90% of marks match to rounding plus 0.7 bp of forward; the worst case is 5.2 bp (26MAR27, where calls and puts agree on the offset). The residual is timing between each ticker's displayed `underlying_price` and the forward behind its mark, not a formula error. The Greeks match Deribit's display rounding.
- **Finding:** for expiries under 7 days, Deribit's displayed theta follows a different convention (a factor of 1.1–10 from the analytic value). Ours is the analytic derivative, checked against finite differences. The project never uses the theta Greek for P&L (attribution uses full revaluation), so this has no effect.

## D017 — Forwards: basis × latest index; interpolate for expiries without a future (not parity)
- **Category:** from data (tested against Deribit)
- **Choice:** `F(t) = basis × index`, with every input strictly before `t − latency`.
  - **Expiries with a dated future:** basis = mark / index from the latest trade of that future (stale after 1 h).
  - **Expiries without a future:** ln(basis) is interpolated linearly in T between the futures, through ln b = 0 at T = 0. Beyond the last future, annualised carry is held flat.
  - Put-call parity on marks is kept as `pricing.forwards.non_future_source: parity` and as a diagnostic.
- **Alternative:** Put-call parity on option marks for expiries without a future, as CLAUDE.md first said.
- **Why:** For each trade, the forward Deribit must have used can be backed out from its trade IV. We did this for diagnosis only; it never feeds the pipeline, so the validation stays non-circular. On 1–7 Jan 2025:
  - future: median gap ±0.07 bp, typical scatter 0.3–0.8 bp
  - interpolation: ±0.2 bp, scatter 0.6–1.0 bp
  - parity: biased by −0.7 to −4 bp, scatter 4–10 bp
  So Deribit's synthetic forward behaves like an interpolation between futures. Parity is hurt by call/put marks up to 60 s apart. Dropping parity lowered the 95th-percentile IV error from 0.41 to 0.37 vol points. The owner chose interpolation (2026-11-09), and CLAUDE.md was updated.
- **Why basis × index rather than the raw last future price:** dated futures can go 10–30 min without a trade (Phase 1). The basis barely moves in that time, while spot can move 1%, and on a 7-day ATM option a 1% forward error is about 9 vol points.

## D018 — Two timing views: inclusive (≤ t) and strict (< t − latency)
- **Category:** from theory (the no-lookahead rule, applied to what happens at t)
- **Choice:** Use all information up to t, and include the information stamped *at* t unless the action at t needs a quote that already existed before t.
  - **Inclusive (≤ t, each trade's own index):** used to validate the pricer against Deribit's trade IV, and to convert marks into mark IVs for Surface A. A refit at time R only uses trades with ts < R, so each trade's own index is known at R.
  - **Strict (< t − latency):** used for anything a resting quote depends on: quote prices and fill decisions. The trade at t is the taker hitting our quote, which had to be in the book before t. Its own index arrives with that trade.
  - The index series is built from every trade stream (perp, futures, options), so the latest index before any t is as fresh as the data allows.
- **Alternative:** Strict everywhere. That would make the Phase 2 test mostly measure index update speed, and it would add timing noise to every short-dated mark IV that feeds the surface.
- **Why:** The Phase 2 check asks whether our pricer and forward method are right, so it should use the inputs Deribit had at the trade instant. The cost of not having them (the market moving just before a trade) is real adverse selection, and it's measured by the latency experiment and the markouts, not hidden in the pricer test. The owner agreed (2026-11-09). On 1–7 Jan 2025, inclusive gave median 0.017 / p95 0.237 vol points, and strict at 0 ms gave 0.030 / 0.369.

## D019 — Phase 2 done-when: a vol target beyond 4 days, plus a price target on everything
- **Category:** free choice (owner decision, 2026-11-09), backed by data
- **Choice:** Both of these must pass, using the inclusive timing view (D018):
  1. For expiries over 4 days, |our IV − Deribit's trade IV| has median ≤ 0.05 and p95 ≤ 0.25 vol points.
  2. On all trades, ≥ 95% are reproduced within ½ tick of price.
- **Alternatives:**
  - The original single vol target on all trades, which failed at p95 0.362.
  - First trying to tighten the interpolated forward, which at best roughly halves the 1–4 day tail and still misses.
- **Why:** Our forward is within 0.33 bp (with a future) and 0.58 bp (interpolated) of the forward Deribit must have used. On 1–4 day options, half a basis point of forward (a fraction of a tick) is worth up to a full vol point. So a vol target there measures the option's sensitivity, not the pricer. Quotes sit on the tick grid, so price error is the error that matters.
- **Result (H1 2025):** PASS. Beyond 4 days: median 0.013, p95 0.142. All trades: 97.2% within ½ tick.
- **Consequence for Phase 5:** for 1–4 day options, small index moves change the IV by about 1 vol point at p95 even at 0 ms latency. Wider spreads or a near-expiry cut-off (`min_hours_to_expiry`) will be needed there.

## D020 — Surface A trailing window: 60 minutes
- **Category:** free choice, within CLAUDE.md's 30–60 min range
- **Choice:** A refit at R uses mark IVs with R − 60 min ≤ ts < R (`surface.trailing_window_s: 3600`).
- **Alternative:** 30 minutes.
- **Why:** Marks only exist where trades happen. With 30 minutes, many expiries beyond a few weeks have too few distinct strikes to fit. Recency weighting (D024) keeps the longer window from making the level stale.

## D021 — Minimum-data ladder: full → carry → none
- **Category:** free choice
- **Choice:** Per expiry and refit:
  - **full:** enough data to fit (rule in D026).
  - **carry:** otherwise, if the last full fit is younger than `max_carry_s` (2 h), keep it, rescaled in time and projected to stay calendar-consistent.
  - **none:** otherwise no smile; neighbouring expiries interpolate.
- **Alternative:** Always fit whatever data there is, or drop expiries without data.
- **Why:** A smile fitted to one or two strikes is arbitrary. Dropping it entirely loses an expiry for a few minutes after every quiet spell. The owner approved it (2026-11-09).

## D022 — Arbitrage: enforce inside the fit, then flag on every refit
- **Category:** from theory, plus evidence
- **Choice:** Butterfly and calendar conditions are built into the fit (D023). Every refit is still re-checked on a k grid (Gatheral g(k) ≥ 0; total variance non-decreasing in T), and flags are reported.
- **Alternative:** Fit free raw SVI and only flag.
- **Why:** With free SVI, close to 100% of refits were flagged. Thinly sampled expiries produced degenerate fits (ρ → −1, m at its bound). A flag-only approach would have left the simulator pricing off arbitrageable smiles.

## D023 — eSSVI slices instead of free raw SVI
- **Category:** from theory (Hendriks & Martini 2019; Gatheral & Jacquier 2014). Owner decision, 2026-11-09.
- **Choice:** Each expiry is an eSSVI slice (θ, ρ, ψ):
  - **Butterfly-free by construction:** ψ ≤ ψ_max(θ, ρ).
  - **Calendar, against the next-shorter expiry fitted at the same refit:**
    - hard bounds θ ≥ θ_short and ψ ≥ ψ_min (wing slopes ordered), which are necessary;
    - plus a stiff penalty on w_long(k) < w_short(k) on a dense k grid.
  - Expiries are fitted short → long.
- **Alternative:** Raw SVI (5 parameters per expiry) with soft penalties.
- **Why:** With 3 parameters, eSSVI fits on fewer strikes and can't go degenerate.
  - The recalled sufficient calendar condition turned out not to be sufficient: a hypothesis property test found a counterexample (`test_wing_conditions_alone_are_not_sufficient`). Hence the grid penalty.
  - Cost: less flexible smiles, so fit error is a little higher than free SVI where data is rich.

## D024 — Recency weighting inside the window: τ = 600 s
- **Category:** free choice
- **Choice:** Weight = vega × exp(−age / 600 s).
- **Alternative:** Equal weights across the window.
- **Why:** In a 60-minute window, a level move in the last 10 minutes would otherwise be averaged with 50 minutes of older marks. Older marks still help pin the smile's shape.

## D025 — Fit guards: retry stuck warm starts, reject absurd fits
- **Category:** free choice
- **Choice:**
  - If a warm-started fit has RMSE > 2 vol points, also run the cold starts and keep the better fit.
  - If the best fit is still > 5 vol points, reject it: the expiry carries or gets none, and it does not constrain longer expiries.
- **Alternative:** Trust the warm start.
- **Why:** In the first full build, one warm start on an 8.6-day expiry got stuck at RMSE 142. Through the calendar bounds it pushed every longer expiry up, which produced 924 spikes in the 30-day ATM.

## D026 — Full fits need identified data; DVOL check on daily changes; per-bucket error targets
- **Category:** free choice (owner decision, 2026-11-09), backed by data
- **Choices:**
  1. **full** needs ≥ 4 distinct strikes (more than the 3 parameters) **and** strikes on both sides of the forward. Otherwise carry or none (D021).
  2. **DVOL check:**
     - the correlation of **daily** changes of the 30-day ATM vs DVOL must be ≥ 0.6;
     - DVOL − ATM must stay in [0, 8] vol points in ≥ 95% of hours.
     The hourly-change correlation is reported for information only.
  3. **RMSE targets per expiry bucket** (median / p95, vol points):
     - 0–2 d: 1.25 / 4.0
     - 2–8 d: 0.75 / 3.0
     - 8–35 d: 0.40 / 2.0
     - 35–100 d: 0.30 / 1.5
     - over 100 d: 0.30 / 1.5
- **Alternatives:**
  - The provisional single target (0.3 / 1.0 on all expiries).
  - An hourly-change correlation ≥ 0.8.
  - Adding more fitting machinery: a level-only rung, or temporal priors.
- **Why:**
  - **Hourly noise came from one place.** The second build still had an hourly correlation of 0.17. Each expiry's fitted ATM matched its own near-ATM marks well (std 0.7 vol points). The noise came from the monthly expiry just past 30 days, which trades 1–4 strikes an hour: 3 strikes give an exact 3-parameter fit at an arbitrary ATM level (e.g. 25-Apr on 2025-03-17: 85 → 52 → 69 within an hour).
  - **The hourly test is too strict for trade-time marks.** A model-free proxy, the last near-ATM mark of any 20–45 day expiry, only reaches 0.60 hourly against DVOL, because hourly DVOL moves (std 0.64 vol points) are small next to the sampling noise of trade marks.
  - **The level band reflects construction.** DVOL is a variance-swap-style index and sits above ATM by construction (mean gap 3.2).
  - **Error targets are tied to the spreads we'll quote.** For quoted expiries, the median error sits below the narrowest half-spread tested in Phase 8 (0.5 vol points), and p95 below the wider ones. The 0–2 day bucket is looser: tiny premiums, and Phase 5 will likely cut it off.
- **Caveat:** these targets were set after seeing the data. They show the surface is fit for its use; they are not an independent test.
- **Result (H1 2025):** PASS.
  - Fit error within target in every bucket (median / p95):
    - 0–2 d: 1.06 / 3.19
    - 2–8 d: 0.69 / 2.48
    - 8–35 d: 0.33 / 1.55
    - 35–100 d: 0.23 / 1.02
    - over 100 d: 0.16 / 0.88
  - 99.1% of normal-day refits arbitrage-free (butterfly 0%, calendar 0.9% of all refits).
  - DVOL tracking:
    - daily-change correlation 0.858;
    - 99.0% of hours with DVOL − ATM in [0, 8] (mean 3.40, std 1.43);
    - hourly-change correlation, for information only: up from 0.17 to 0.38.
  - Cost: more expiries over 100 days carry or have no smile (39% none). This is acceptable, because quoting is limited to a shorter range anyway (Phase 5).
  - A few isolated one-refit spikes remain in the 30-day ATM, RR25 and BF25 series (e.g. early Feb, late Jun). Phase 5 should be robust to a single bad refit.
