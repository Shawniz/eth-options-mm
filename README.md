# ETH Options Market-Making Simulator

A research simulation of an ETH options market maker on Deribit. It fits a vol surface, quotes around it, gets filled by **real historical Deribit trades**, delta-hedges with the perpetual, enforces Greek limits and attributes P&L.

**Research question:** which quoting and risk parameters make money after adverse selection, hedging costs and fees, and why?

> Work in progress. Built in phases; see `CLAUDE.md`.

## Quick start

```bash
uv sync
uv run pytest
uv run python -m mm.run --config config/base.yaml --start 2025-01-01 --end 2025-01-02
```

Python 3.13 · numpy · scipy · pandas · pyarrow · duckdb · httpx · pydantic

Data comes from Deribit's public history API and is cached locally under `data/`. It is not committed.
