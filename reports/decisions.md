# Modelling and engineering decisions

Each entry: the choice, the alternative, and why.

---

## D001 — Python 3.13 instead of 3.12
- **Choice:** Python 3.13 (newest Homebrew install, 3.13.5).
- **Alternative:** 3.12, as first written in CLAUDE.md.
- **Why:** The owner asked for the newest local Python. All core libraries support 3.13.

## D002 — Strict config validation
- **Choice:** Pydantic models with `extra="forbid"`. Out-of-range values fail at load.
- **Alternative:** Plain dicts read from YAML.
- **Why:** A misspelt parameter would otherwise fall back to a default without anyone noticing and change results.

## D003 — `at_price_fill_fraction` defaults to 0.0
- **Choice:** Trades exactly at our quote price do not fill us in the base config.
- **Alternative:** A partial fill, for example 0.25 to 0.5.
- **Why:** Price-time priority. A quote strictly better than p would have been hit first, but at p we tie with resting orders whose queue position is unknown without order-book data. Small trades usually only fill the front of the queue, and the ties that do reach deep into the queue tend to be the most adverse. We use 0.0 (the pessimistic bound) for all runs. The owner decided not to sweep it. It stays a config parameter so it can be changed later without code changes. *Confirmed by owner.*

## D004 — Study period 2025-01-01 to 2025-06-30
- **Choice:** Six months of UTC days, start inclusive and end (2025-07-01) exclusive. Set in `config/base.yaml` under `study_period`.
- **Alternative:** A shorter window, or a whole year.
- **Why:** It covers calm stretches and stress periods (the early-February crash and the April tariff selloff), which leaves room for a time split. It's about 0.5 GB on disk, measured.

## D005 — Exclude combo trades as well as block trades
- **Choice:** Drop trades with `combo_id` (and with `block_trade_id`). Each is controlled by a flag in `data.exclude_*`.
- **Alternative:** Keep combo legs as fills.
- **Why:** Combo legs match on the combo book at prices allocated to each leg. They are not hits on a single-leg quote, so counting them as fills would inflate our fill rate. Owner agreed.

## D006 — Keep liquidation trades
- **Choice:** Keep trades with a `liquidation` flag and mark them `is_liquidation`.
- **Alternative:** Drop them.
- **Why:** They execute against the order book, so they could have hit our quote. They are also realistic adverse flow. The flag allows analysing them separately.

## D007 — Raw storage is lossless; one file per UTC day
- **Choice:** `data/raw/<source>/date=YYYY-MM-DD/part.parquet`. Known fields are typed columns, and any field we don't recognise goes into `extra_json`. Each day is written once, atomically, and never overwritten. Days that aren't finished are refused. Instrument specs are snapshots per fetch date.
- **Alternative:** Store raw JSON, or append to a single file.
- **Why:** Raw stays an exact record of what the API returned. Re-runs are cheap because cached days are skipped.

## D008 — Paging and inclusive bounds
- **Choice:** Each day is requested as `[00:00:00.000, 23:59:59.999]` (both bounds were checked to be inclusive). Pages advance from the last timestamp seen, trades are de-duplicated by `trade_id`, and the download fails loudly if a full page shares one millisecond.
- **Alternative:** Skip past the boundary millisecond. That would silently drop trades.
- **Why:** The API pages by timestamp only, so trades at the boundary come back twice.

## D009 — DVOL is hourly and carries `available_at`
- **Choice:** Hourly DVOL candles from the main API. `available_at = ts + 1h` is the time the candle's close becomes known.
- **Alternative:** 1-minute candles. These are not available historically (the history server has no DVOL at all).
- **Why:** Hourly is enough for regime labels. `available_at` prevents lookahead: candles are stamped at their open, but the close is only known an hour later.

## D010 — Futures and perp downloaded together, split when cleaning
- **Choice:** A single `kind=future` download, which includes ETH-PERPETUAL, split into `perp_trades` and `future_trades` during cleaning. Sizes are stored as `amount_usd`, because these are reversed contracts with USD notional.
- **Alternative:** A separate per-instrument download for the perp.
- **Why:** It's one paging loop and the result is identical. Note that there are no daily futures: forwards for daily option expiries will need interpolation (Phase 2).
