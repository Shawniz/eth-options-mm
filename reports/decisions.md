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
