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
- **Why:** This is the most conservative assumption, because queue priority is unknown without order-book data. It will be swept in Phase 8. *Pending owner confirmation.*
