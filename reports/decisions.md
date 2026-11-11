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

## D027 — Fill size: expected (fractional) contracts
- **Category:** free choice (owner decision, 2026-11-10)
- **Choice:** Fill size = min(trade size, what is left of our quote) × `max_fill_fraction`, kept fractional (`sim.fill_sizing: expected`). A 1-contract trade that crosses our quote fills 0.5 contracts.
- **Alternatives:**
  - Seeded random whole contracts with the same mean (`fill_sizing: random`, keyed by trade_seq so every variant sees the same draws). Kept for the Phase 8 robustness check.
  - Rounding down to whole contracts: rejected. 53% of option trades are a single contract, so it would silently drop most of the flow.
- **Why:**
  - Without order-book data there is no queue position to simulate. The trade-through rule plus a participation fraction is the usual conservative convention.
  - P&L, fees and Greeks are linear in size, so expected size gives the same average as the random version without its noise. That suits paired comparisons and CLAUDE.md's determinism rule.
  - Cost: logged sizes are not real order sizes.

## D028 — Exchange pauses: no quotes until the next refit
- **Category:** free choice, from the Phase 1 data notes
- **Choice:** A gap of more than `sim.pause_gap_s` (120 s) with no trade on any stream is a pause. Quoting stays off from the end of the gap until the first surface refit after it.
- **Alternative:** Ignore pauses (listed in extensions.md as a possible simplification).
- **Why:** After a pause the underlying, forwards and surface inputs are stale. The rule is cheap to apply. On 6–12 Jan 2025 it caught 12 gaps and blocked 38 trades.

## D029 — Simulator conventions (Phase 4)
- **Category:** free choice, except where noted
- **Choices:**
  - **Suspect trades fill us** (owner decision, 2026-11-10). Real bad data was already dropped in Phase 1. A suspect trade really printed, and a better resting quote would have been hit first. We fill at our own quote, so a strange print cannot hand us a windfall. Fills carry the flag.
  - **Sweeps:** one taker order sweeping several levels arrives as several trades at the same timestamp. Our quote is used up across them and is replenished only at a trade after fill time + latency. Otherwise one sweep could fill us several times over.
  - **Quotes only for expiries with their own slice** (full or carry) at the latest refit. No interpolated smiles, and no older slice if the latest refit has none.
  - **Between refits**, the slice's implied vol at the current moneyness, k = ln(K/F) with F the latency-shifted forward, is held fixed and priced at the trade's own time to expiry.
  - **Refit timing:** a refit stamped at R uses marks before R, so it is usable by quotes at R or later (refit → quote tie-break, CLAUDE.md).
  - **Perpetual trades are not loop events in Phase 4.** The underlying comes from the forward curve. Perp trades enter the loop in Phase 6, with hedging.
  - **Daily options** for the delivery-fee exemption (D012, unverified) = expiries on a weekday other than Friday.
- **Observed on 6–12 Jan 2025 (placeholder quoter, ±2 vol points):**
  - **Volume share:** we fill 4.7% of trades and 0.78% of contracts; 20/20 hand-checked fills match.
  - **Wings:** in the far wings (more than 2 standard deviations from the forward), our fair vol at fills is a median 5.5 vol points below Deribit's mark. The 3-parameter eSSVI smile can't follow the steep put wing, and vega weighting gives it little weight in the fit. Because fills concentrate where we are mispriced, that's where it shows. Phase 5's minimum |delta| universe rule should keep us out of those strikes.

## D030 — Quoting universe: tenor ≥ 2 days, OTM delta ≥ 0.10
- **Category:** free choice, backed by data (training week 6–12 Jan 2025; owner decision, 2026-11-10)
- **Choice:**
  - Quote expiries with `min_days ≤ T ≤ max_days` (2 to 90 days). CLAUDE.md's `min_hours_to_expiry` is the same rule as `min_days` once both apply per expiry, so only `min_days` is kept.
  - Quote a strike only if its **OTM delta**, min(|Δ_call|, |Δ_put|) at fair vol (Black delta), is ≥ `min_abs_delta` = 0.10. OTM delta, not the option's own |delta|, because a deep ITM call sits on the same mispriced put wing as the OTM put at that strike.
- **Alternatives:** 1-day cut-off (Phase 4 placeholder); a plain |delta| rule; a 0.05 threshold.
- **Evidence:** Phase 4 fills compared with Deribit mark IV (fair = Surface A slice at k = ln(K/F_q), latency 0; mark IV from the priced cache). Median fair − mark (vol points), p90 of |gap| in brackets:
  - more than 4 days: below 0.05 delta −5.64 (13.7); 0.05–0.10 +2.09 (5.5); 0.10–0.20 +2.88 (6.9); ≥ 0.20 −1.04 (4.1)
  - 1–2 days: at ≥ 0.20 delta the p90 is still 7.5; below 0.05 delta it is 15.0
  - Half of all Phase 4 fills came from strikes below 0.05 delta.
- **Why:** Below 0.05 delta the surface is clearly wrong. **0.05–0.10 is borderline** (its gap is no worse than 0.10–0.20 on this sample). 0.10 was chosen to leave a margin, not because the data force it. 1–2 day options are noisy even near the money, well past any spread we would quote.
- **Caveats:**
  - One week of training data, a few hundred fills per bucket.
  - The mark is Deribit's model, not the truth.
  - Fair vol comes from a refit up to 5 minutes old, while the mark is at the trade, so part of the gap is staleness.

## D031 — Price floor and rounding
- **Category:** free choice (`min_half_width_ticks`, `fee_cover`); tick and fee from specs
- **Choice:** Around the skewed centre price P_c = P(σ_c), each side is at least
  half_min = max(`min_half_width_ticks` × tick(P_c), `fee_cover` × maker fee(P_c)) away:
  bid = round_down(min(P(σ_c − h), P_c − half_min)), ask = round_up(max(P(σ_c + h), P_c + half_min)).
  Defaults are 1 tick and 1 × fee, so each fill at least pays its own fee relative to the centre. No bid if it would fall below the minimum tick. Quotes stay at least one tick wide.
- **Alternative:** a floor around fair rather than around the skewed centre; a fee floor only.
- **Why:** The floor is about width, not edge. Skew is meant to move the whole quote, so the floor moves with it. Rounding down the bid and up the ask never improves our price beyond the model's.
- **Consequence:** when the skew sits at its cap (±h, D032), the inner quote can be exactly at fair. Such a fill has zero edge against fair and pays the maker fee, so it loses the fee. That is the intended price of unloading inventory: spread capture against fair stays ≥ 0, but net of fees it can be slightly negative.
- **Observed (6–12 Jan 2025, h = 1 vol point):** the floor sets a side at 47% of fills: 90% at 2–8 days (median vega $0.92 per contract, so 1 vol point is less than a 0.0005 tick), 15% at 8–35 days, 0% at 35–100 days. **Phase 8 note:** at short expiries the narrow spread settings (0.5 and 1 vol point) are the same quote; the spread experiment mainly moves the longer expiries.

## D032 — Inventory skew on total book vega, capped at the half-spread
- **Category:** free choice (owner decision on the cap, 2026-11-10)
- **Choice:** Δσ = clip(−κ · V / V_ref, −h, +h) and σ_c = σ_fair + Δσ, where V = Σ nᵢ · Black vegaᵢ · S/F (USD per vol point) is the book's vega.
  - V is recomputed at each refit from information before the refit (each expiry's latest live slice, forwards strictly before R), updated by each fill's vega, and recomputed after expiries.
  - Defaults: κ = 1 vol point, V_ref = $5,000 per vol point (Phase 6 ties V_ref to the vega limit).
- **Alternatives:** no cap (the inner quote can cross fair, so we pay to unload); a separate `max_skew_volpts` cap.
- **Why:**
  - Shifting by vol points moves each option's price by Δσ × its own vega, so the shift scales with the vega each fill adds or removes. This is the vega analogue of an Avellaneda–Stoikov reservation price.
  - The cap at h means we never quote through fair:
    - spread capture stays non-negative against the centre;
    - the Phase 8 spread and skew runs don't partly measure crossing fair;
    - extremes are left to the Phase 6 vega limit.
  - Cost: strong κ saturates earlier.
- **Known simplification:** total vega nets front-month against back-month (term-structure risk is ignored; vega buckets are in extensions.md).
- **Observed (6–12 Jan 2025, unhedged):** net vega at fills ranged from −$1,279 to +$674 per vol point, so |skew| ≤ 0.26 vol points (median 0.10) and the cap never bound. Skews below about 0.2 vol points are often hidden by tick rounding on short expiries.

## D033 — Capped vega-normalised sizing
- **Category:** free choice
- **Choice:** size = floor(min(`target_vega_usd` / vegaᵢ, `max_contracts`)) to the order increment, where vegaᵢ = Black vega × S/F (USD per vol point per contract). No quote below the minimum order. `sizing: fixed` (`fixed_contracts`) is kept for the Phase 8 robustness check. Defaults: $50 per vol point, cap 25 contracts.
- **Alternative:** fixed contracts (the Phase 4 placeholder).
- **Why:** Every quote offers about the same vega risk, so a fill in a 60-day option and a fill in a 5-day option move the book's vega by similar amounts. The cap stops low-vega short expiries from being quoted in very large size.
- **Observed (6–12 Jan 2025):** the cap binds on 60% of fills (median size 25 at 2–8 days, 22 at 8–35 days, 11 at 35–100 days). In practice short expiries are fixed-size at the cap. Whether to raise the cap or lower the target is a Phase 8 sensitivity, not tuned here.

## D034 — Account delta and hedging the collateral
- **Category:** from theory (CLAUDE.md formula); hedging the collateral is an owner decision (2026-11-10)
- **Choice:**
  - Δ = B + Σ nᵢ·N(d1ᵢ) (N(d1) − 1 for puts) + Q/P0, in ETH (USD per $1 move in ETH).
  - The hedger targets Δ = 0, so the initial 50 ETH collateral is hedged too: the first hedge sells about $150k of perp.
  - Option deltas use Black delta at the Surface A vol at the current moneyness (sticky moneyness between refits, as for quotes), with F = basis × S and S from the latest perp print's index.
- **Alternatives:** hedge only the option book (so P&L in USD carries 50 ETH of spot exposure); hold the collateral in USD.
- **Why:** P&L and risk are in USD (CLAUDE.md), so unhedged collateral would swamp the options P&L with spot moves. The short perp that hedges the collateral earns or pays funding. That carry is the same for every variant and is reported as its own line (Phase 7).
- **Expiry jumps:** at settlement, an ITM option turns into a fixed ETH amount in B. Its delta jumps from ±1 per contract to that ETH amount (e.g. 10 Jan 2025: about 110 ETH in one print), and the next check hedges it. This is real for coin-settled options, not a bug.

## D035 — Limits: shrink each side to the room left; V_ref = the vega limit
- **Category:** free choice (owner decisions, 2026-11-10)
- **Choice:**
  - |net vega| ≤ `vega_limit_usd` ($2,000 per vol point) and |nᵢ| ≤ `max_position_contracts` (100).
  - Each side's quote size is cut to the room left under both limits (bid room = min(cap − n, (L − V)/vegaᵢ); ask room = min(cap + n, (L + V)/vegaᵢ)). A side below the minimum order is not quoted.
  - The skew scale is the vega limit itself: Δσ = clip(−κ·V/L, ±h). `quoting.vega_ref_usd` is removed, so κ is "the skew at the limit".
- **Alternatives:** a fixed "near" threshold (e.g. drop the side above 90% of the limit); a separate V_ref.
- **Why:**
  - A fill is never larger than the quote, so fills alone can never breach a limit (property test). Only drift (vega moving with spot and time) can breach, and then only the risk-reducing side has room. That is CLAUDE.md's "near a limit, quote only the risk-reducing side" with no extra threshold to choose.
  - $2,000: a 10 vol point shock costs about $20k, roughly 12% of the 50 ETH ($165k) capital. 100 contracts is 4× the maximum quote size.

## D036 — Hedge execution: next same-side perp print after the latency, plus 1 bp
- **Category:** free choice, backed by data (owner decision, 2026-11-10)
- **Choice:**
  - Trigger: |Δ| > `delta_band_eth` (5 ETH), checked after every perp print and every option fill.
  - Size: ΔQ = −S·Δ, whole $1 contracts.
  - Execution: the first perp print **on our side** (a sell hedge needs a taker-sell print, i.e. the bid) **strictly after** the trigger with ts ≥ trigger + latency.
  - Price: print × (1 ± `hedge_cost_bps` = 1 bp). Fee: taker 0.05% × |ΔQ| / price, in ETH.
  - At each hedge the open perp P&L is settled into B and P0 resets to the execution price (delta unchanged, unit test). No new hedge is decided while one is pending.
- **Alternatives:**
  - The next print of any side: rejected. Prints alternate between bid and ask (consecutive opposite-side prints differ by a median 0.9 bp, 27 Jan – 10 Feb 2025), so a sell hedge would often fill at the ask.
  - The decision print itself: that is lookahead.
  - Maker hedging at the 0% fee: CLAUDE.md fixes taker hedging; this is an extension.
- **Why:**
  - Same-side prints pay the spread a real taker pays.
  - The 1 bp covers size. Median print $476, p99 $27k, while a typical hedge is $15–20k.
  - Same-side prints come every 3 ms (median), p99 about 25 s; long gaps happen only in exchange pauses.

## D037 — Phase 6 results and what they leave for Phase 7
- **Category:** results (no parameter changed after seeing them)
- **Checks (done-when):**
  - Unit tests pass: hedge sign and rounding, inverse-perp P&L and Q/P0 delta by bump, delta and value unchanged at perp settlement, funding sign on the short hedge, premium-adjusted delta through B, limits never breached by fills (property test), Black delta = bump-and-revalue on a flat smile.
  - **Stress run, 27 Jan – 10 Feb 2025** (training data; the 3 Feb crash took ETH from about 2,870 to 2,150 and back to 2,880):
    - |Δ| ≤ 5 ETH in 99.30% of the time and 99.85% of 1.88M checks;
    - 2,868 hedges (205 a day, $46.6M of perp), taker fees 8.56 ETH, slippage 1.71 ETH;
    - wait from decision to the same-side print: median 0.24 s, p99 43 s;
    - the largest |Δ| (136, 98, 88 ETH) are all 08:00 expiry settlements (D034).
  - **Limits visibly cut quoting:** net vega touched both −$2,000 and +$2,000 on 3 Feb. 1.5% of quotes were one-sided overall, with hourly peaks at 47% (see `reports/figures/sim_2025-01-27_2025-02-10_vega.png`).
  - 20/20 hand-checked fills match on both the 6–12 Jan and stress runs, including limit-cut sizes.
- **Results that need Phase 7:**
  - **Hedging costs are as large as the edge.** 6–12 Jan: 1,015 hedges, 2.90 ETH fees + 0.58 ETH slippage, against +3.3 ETH of edge at fill. Only 109 hedges were triggered by fills; the rest came from gamma on short-dated inventory held into expiry.
  - **The stress fortnight lost 73% of equity in USD** ($159k → $35k). 3 Feb alone: options +23.6 ETH (premium + settlement + value change), perp hedge −45.9 ETH, fees −5.5 ETH. Two contributors were measured; their split is for the Phase 7 waterfall and markouts:
    1. **Smile delta left unhedged.** On the 2 Feb book, Black delta is +0.8 ETH but the sticky-moneyness bump delta is +18.3 ETH. Between refits Surface A values options at the current moneyness, so a risk-reversal book (long puts, short calls; net vega $352, gross $3,474) has spot exposure that Black delta does not see. CLAUDE.md fixes Black delta; smile-adjusted delta is an extension.
    2. **Crash flow against a stale surface.** 7,716 contracts traded on 3 Feb, half the fortnight, at a surface refitted every 5 minutes from a 60-minute window. The +7.6 ETH "edge" is measured against that same surface, so it may be adverse selection. Delta-hedged markouts will show.
  - **Update (same day, D038):** contributor 1 was tested by hedging the smile-adjusted delta. It changed almost nothing (equity at end $34.0k vs $35.1k; perp P&L −115.7 vs −118.7 ETH). A quick delta-hedged markout check (fair at Surface A at t + h; hedge = Black delta at fill × spot change) points to contributor 2:
    - whole fortnight: edge at fill +$40k, markout +$31k at 1 min, −$3.6k at 5 min, **−$87k at 1 h**;
    - 3 Feb alone: +$19.6k at fill, **−$76.8k at 1 h**, about the whole day's loss.
  - The stress loss is **adverse selection**: crash-day takers traded against a surface up to 5 minutes stale and were right. The proper markout tables (by horizon, expiry and delta bucket) are Phase 7 work.
- **Also:** the perp traded at a discount to the index in the crash (median −10 bp at our hedges, p1 −309 bp). Hedge sizes use the index, so this shows up as hedge cost and residual delta.

## D038 — Hedge the smile-adjusted (sticky-moneyness) delta
- **Category:** from theory (owner decision, 2026-11-10)
- **Choice:** per-option delta for hedging and account delta:
  δᵢ = N(d1) [− 1 for puts] − φ(d1)·√T·σ′(k), with σ′(k) = w′(k)/(2σ·t_ref).
  This is Black delta plus vega × dσ/dS under the sticky-moneyness surface of D029 (dk/dS = −1/S; the S/F factors cancel). `hedging.delta: black` keeps the old rule for comparison.
- **Alternative:** Black delta only (CLAUDE.md's original rule, then listed as an extension).
- **Why:**
  - Quotes and valuation hold the smile fixed in moneyness between refits (D029), so a hedge that holds σ fixed contradicts our own model.
  - On the 2 Feb 2025 book, Black delta was +0.8 ETH but the bump-and-revalue delta was +18.3 ETH. With the new delta, formula and bump agree exactly (−0.61 vs −0.61, real book; plus a unit test on a skewed smile).
  - Black delta is still used to *locate* strikes (quoting universe OTM delta, 25-delta RR/BF in Phase 3).
- **Effect:**
  - 6–12 Jan: 1,226 hedges (was 1,015), fees 3.54 ETH (was 2.90).
  - Stress fortnight: 3,497 hedges (was 2,868), equity at end $34.0k (was $35.1k).
  - |Δ| stays in the band in 99.0% of the time in both runs.
  - It is the consistent choice, not a fix for the stress loss (see the D037 update).
- **To test in Phase 7:** whether Deribit's marks actually move closer to sticky moneyness or sticky strike from refit to refit (regress vol changes at fixed k and at fixed K on spot changes). If marks behave like sticky strike, this delta over-hedges.

## D039 — P&L attribution: inclusive valuation, a basis step, replay from logs
- **Category:** free choice (owner decisions, 2026-11-10)
- **Choices:**
  - **Valuation uses information up to and including t** (index, forward basis, latest refit, perp mark of the latest print). It is measurement, not a decision. Decisions keep the latency rule: a quote uses information before t − latency, a hedge executes ≥ decision + latency (checked by an integration test at 5 s latency). The engine's daily snapshots use the same inclusive rule.
  - **Waterfall per interval, in the order time → spot → basis → vol:** theta, spot (split into delta = Δ·dS with Δ by central bump of the full account at (t1, S0), and gamma = the rest), basis (futures basis and perp premium), vega (the refit).
  - **Basis** is a fourth step, not part of vega or the residual. F = basis × S, and the perp trades at a premium to the index, so both move independently of S.
  - **Revaluation points:** every minute plus every fill, hedge, expiry and funding time.
  - **Flows at events,** in USD at that moment:
    - spread capture = side·q·S·(fair − price), against valuation fair;
    - hedge cost = ΔQ·(1/P_exec − 1/P_mark)·S;
    - option, perp and delivery fees;
    - funding;
    - settlement difference = payoff − model value.
  - **The account is replayed** from fills, hedges and the ledger, independently of the engine, and checked against the engine at every daily snapshot. Residual = engine valuation − sum of components.
  - **Markouts:** the fill's own USD contribution, side·q·S(t+h)·(fair(t+h) − p) (the premium paid in ETH also revalues). Delta-hedged: minus side·q·(δ − p)·(S(t+h) − S(t)), with δ the smile delta at the fill. At h = 0 this equals spread capture.
- **Alternatives:** strict valuation (before t), which matches the engine's old snapshots but books inter-print moves as hedge cost; basis folded into vega.
- **Results:**
  - **6–12 Jan 2025:** total −$20.7k, residual $0.0000, replay = engine at all 7 snapshots. Theta +20.6k, gamma −19.6k, vega −10.9k, spread capture +10.7k, hedge cost −4.1k, fees −15.7k (perp −11.7k), basis −1.8k. Delta-hedged markouts: +$10.7k at fill, −$3.3k at 1 h; every expiry bucket is negative at 1 h.
  - **27 Jan – 10 Feb 2025:** total −$127.6k, residual $0.0000. Vega −72.3k, gamma −47.7k, spread capture +39.2k, hedge cost −19.0k, fees −42.3k, theta +15.7k.
  - **Worst day, 3 Feb:** −$84.3k, most of it 01:00–05:00 during the crash (vega −57k, gamma −14k, hedging −20k, spread capture +20k). All 10 worst fills by 1 h markout are puts we sold into the crash, so the book was short vega as the refits marked vol up. Delta-hedged markouts: +$39.2k at fill, −$3.8k at 5 min, −$85.1k at 1 h.
  - Staleness (logged edge − spread capture at the inclusive fair) is small at latency 0: −$575 over the fortnight.

## D040 — Smile dynamics: Deribit marks are close to sticky strike over an hour (D038 questioned)
- **Category:** result (training period 2025-01-01 to 2025-04-28)
- **Method:** for pairs of refits h apart, with the expiry fitted `full` at both, regress the change in ATM vol through the origin on skew × ln(F1/F0). β = 0 is sticky moneyness (the D038 hedge delta); β = 1 is sticky strike (Black delta). Non-overlapping pairs. `uv run python -m mm.pnl.smile_dynamics --config config/base.yaml`, written to `reports/smile_dynamics.md`.
- **Result (β, ±2 se):**

  | horizon | 2–8 d | 8–35 d | 35–100 d |
  |---|---|---|---|
  | 5 min | 0.24 ± 0.03 | 0.25 ± 0.05 | −0.05 ± 0.16 |
  | 15 min | 0.42 ± 0.05 | 0.57 ± 0.08 | 0.18 ± 0.25 |
  | 1 h | 0.86 ± 0.09 | 0.98 ± 0.13 | 1.06 ± 0.35 |
  | 4 h | 1.36 ± 0.16 | 1.56 ± 0.17 | 1.54 ± 0.45 |
  | 24 h | 1.10 ± 0.89 | 0.84 ± 0.49 | 0.64 ± 0.93 |

- **Reading:**
  - Over an hour or more, ATM vol moves along the old smile, as under sticky strike, or by more (spot-vol correlation beyond the skew).
  - The low short-horizon β is most likely Surface A itself lagging: a 60-minute trailing window with a 10-minute recency weight (D020, D024) can't move the ATM level within 5 minutes.
  - So D038's sticky-moneyness hedge assumes β = 0 where the data say about 1 over the horizons a hedge is held. **Open decision for the owner**, with options:
    1. revert to Black delta (β = 1);
    2. a β-weighted delta, N(d1) − φ(d1)·√T·(σ′(k) − β·σ′(0)), with β from this training-period table;
    3. keep D038.

## D041 — Back to Black delta for hedging (reverses D038)
- **Category:** from data (owner decision, 2026-11-10)
- **Choice:** `hedging.delta: black`. The per-option hedge delta is N(d1) (N(d1) − 1 for puts), and markouts are hedged with the same delta. The D038 sticky-moneyness delta stays available as `hedging.delta: smile`.
- **Alternatives:**
  - keep D038 (sticky moneyness, β = 0);
  - a β-weighted delta, N(d1) − φ(d1)·√T·(σ′(k) − β·σ′(0)), with β estimated on the training period.
- **Why:**
  - D040 measured how Deribit's marks move. Over the horizons a hedge is held, ATM vol moves along the old smile as sticky strike predicts: β ≈ 0.98 at 1 h and 1.56 at 4 h (8–35 d expiries). Under sticky strike Black delta is the right hedge, and D038's correction pushes it the wrong way.
  - D038 had been chosen for consistency with how Surface A is stored (moneyness), not from evidence about the market.
  - The β-weighted delta with β ≈ 1 is almost the same as Black and adds a fitted parameter.
- **Not changed:** valuation between refits is still sticky moneyness (D029). The surface is refitted to marks every 5 minutes, so over longer horizons the market's own behaviour takes over.
- **Effect** (smile delta → Black delta, reruns of both periods):
  - 6–12 Jan: hedges 1,226 → 1,015, perp fees −$11.7k → −$9.6k, delta line +$0.1k → −$1.9k, total −$20.7k → **−$20.0k**.
  - 27 Jan – 10 Feb: hedges 3,497 → 2,868, perp fees −$28.9k → −$23.3k, delta line −$2.4k → −$8.6k, total −$127.6k → **−$126.5k**.
  - Residual $0.0000 on both; 20/20 hand-checked fills on both.
  - The delta line worsens because the attribution values the book sticky-moneyness between refits (D029), so it sees the smile delta that Black leaves unhedged. Within a refit that is the known inconsistency; it costs less than the extra hedging it saves.
  - The stress loss is adverse selection (D037, D039) and barely moves.

## D042 — Phase 8 design: select on train, report on test; four experiments
- **Category:** free choice (owner decisions, 2026-11-10; the owner accepted the recommended settings)
- **Periods:** train 2025-01-01 to 2025-04-28 (65%), test 2025-04-29 to 2025-06-30.
- **Experiments** (one parameter at a time; the baseline is `config/base.yaml`):
  - spread h ∈ {0.5, **1**, 2, 3} vol points;
  - skew κ ∈ {0, **1**, 3};
  - hedge band ∈ {2.5, **5**, 10, 20} ETH (added after the discussion of hedging costs vs gamma);
  - latency ∈ {**0**, 1 s, 5 s}.
- **Selection:** for spread, skew and band, the best value is chosen on **train** by mean daily P&L (Sharpe is reported alongside). The combination is "best". Latency is a condition, not a choice: test only.
- **Robustness on test, applied to "best":** stricter fills (`max_fill_fraction` 0.25); looser fills (1.0, plus `at_price_fill_fraction` 0.5, the only relaxation of D003); fixed 10-contract sizing; random whole-contract fills (D027).
- **Stress day:** daily mean DVOL ≥ the 80th percentile of **training** days.
- **Comparison:** paired daily differences against the baseline, mean ± 2 standard errors, with the lag-1 autocorrelation reported as a warning that the error bar may be too narrow.
- **Reproduction:** one command, `uv run python -m mm.run --config config/base.yaml --experiments config/experiments.yaml`. Each run is cached by a hash of its resolved config, so an interrupted batch resumes.
- **Lean grid (owner, 2026-11-10):** the project should show the method, not an exhaustive search, on an 8 GB laptop. So: spread {0.5, **1**, 2}, κ {0, **1**}, band {**5**, 10}, latency {**0**, 1 s}; robustness = stricter and looser fills only. That is 14 runs (5 train, 9 test), with training runs one at a time (about 5 GB each) and test runs two at a time. The dropped values (spread 3, κ 3, bands 2.5 and 20, latency 5 s, fixed and random sizing) can be added back to `config/experiments.yaml`; cached runs are reused.

## D043 — A cash-gamma limit
- **Category:** from data (training period) plus a free choice (the 12% budget, the same as the vega limit); owner decision, 2026-11-10
- **Choice:** |net cash gamma| ≤ `gamma_limit_usd` = $115, where cash gamma = the USD gamma P&L of a 1% ETH move, ½·Γ_S·(0.01·S)² summed over the book (Γ_S = basis × Black gamma). It is enforced like the vega limit (D035): each side's size is cut to the room left; buying adds +gᵢ, selling −gᵢ. Net gamma is recomputed at each refit and updated at each fill.
- **Calibration:** the training-period 99th percentile of |daily ETH return| is 13.3%. A move that size costs 13.3² × G₁, and the budget is 12% of starting capital (50 ETH × $3,353 = $167.6k), so G₁ ≤ 0.12 × 167.6k / 13.3² ≈ $115.
- **Alternatives:** vega buckets by tenor; no gamma limit.
- **Why:** a 3-week smoke run on May 2025 lost $232k on about $90k of capital. During the rally takers bought calls and the book went heavily short **short-dated** gamma (2–8 days: tiny vega, large gamma). The net-vega limit could not see it.
- **Effect:**
  - May smoke run: −$232k → −$114k.
  - 6–12 Jan: hedges 1,015 → 220, fills 1,252 → 1,095.
  - Known gap: the limit only shapes new quotes. Gamma rises by itself as positions approach expiry (Γ ∝ 1/√T), so the book can drift above the limit. In May, mean |gamma| was 104% of the limit and 59% of quotes were cut. Active reduction (unwinding short-dated inventory) would need trading out of positions, which is an extension.

## D044 — Exchange order for same-millisecond trades
- **Category:** bug fix (found by the hand-check, 2026-11-10)
- **Problem:** data loads ordered trades by `ts` only. Trades in the same millisecond came back in an undefined DuckDB order, and they can carry different index snapshots (e.g. 2025-01-09 08:21:06.648: 3,281.58 and 3,281.64). So "the latest index before t" was ambiguous, and runs were not guaranteed to be deterministic. One of 20 hand-checked fills failed on it.
- **Fix:** Deribit trade ids are one global sequence across all instruments, so trades are ordered by (ts, trade number) everywhere: the curve's index series, futures basis observations, option and perp loads, and the engine's event sort. The hand-check applies the same rule in its own SQL.
- **Result:** hand-check 20/20, and a wider 200/200; a rerun of 6–12 Jan is byte-identical.
- **Not rebuilt:** the Phase 2 priced cache and the Phase 3 surface cache. Each mark uses its own trade's index, so the tie only touches forward lookups at sub-tick precision.

## D045 — Phase 8 results; replay start without an index print
- **Category:** results (test period 2025-04-29 to 2025-06-30) plus a bug fix (2026-11-11)
- **Selected on train:** half-spread 2 vol points, skew off (κ = 0), hedge band 10 ETH.
- **Test results** (per day, paired against the baseline, ± 2 se):
  - spread 2: +$991 ± 603;
  - band 10: +$348 ± 102, the most precise result (lag-1 autocorrelation 0.38, so the bar is about 1.5× too narrow);
  - skew off: −$1 ± 564;
  - spread 0.5: −$478 ± 443;
  - latency 1 s: −$153 ± 172;
  - best: +$1,241 ± 908.
- **Every variant loses money:** the baseline −$189k, best −$111k. Fills enter with 1.1–2.4 vol points of edge, but the delta-hedged 1 h markout is −0.4 to −1.1 vol points (adverse selection), and short gamma plus hedging costs exceed spread capture. The mechanism for each experiment is in `reports/phase8_mechanisms.md` (appended to `reports/phase8_results.md`).
- **Bug found by the batch:** the training summaries had a NaN total.
  - 2025-01-01 00:00 is the first instant of the data, so there is no index at the start; `w_start` was NaN, and the first waterfall step (to the first collateral hedge, about 5–20 s in) was NaN, which the flow fill-in read as 0.
  - Effect on training: about 50 ETH × the first-minute move ≈ −$26 on day 1, identical in every variant (no options yet; the same collateral hedge). Selection is unchanged.
  - Test runs start on 29 April, after earlier data, and were unaffected (residual ≈ 1e-9).
- **Fix:**
  - The replay starts at the first index print if there is none at the start. This is valid because only collateral exists before it and nothing can trade.
  - A non-finite waterfall step now raises an error instead of reading as 0.
  - The run summary's total comes from the replay, with `engine_minus_replay_usd` recorded as a check.
  - Tests cover both cases and fail on the old code.
- **Not rerun:** the five cached training runs (about 2 h on the laptop). Their `daily_pnl` differs from a fresh run only by that common ≈ $26 on day 1, and their `summary.json` totals are NaN. The cache key is the config hash, so to regenerate them, delete `data/runs/exp/train/`.
