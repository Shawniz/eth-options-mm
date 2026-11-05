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
