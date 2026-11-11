# Project guide: ETH options market-making simulator

A research simulation of an ETH options market maker on Deribit. It fits a vol surface to Deribit mark prices, quotes bids and asks around it, gets filled by **real historical Deribit trades** under a strict trade-through rule, delta-hedges with the inverse perpetual, enforces vega, gamma and position limits, and attributes every dollar of P&L.

**Research question:** which quoting and risk choices make money after adverse selection, hedging costs and fees, and why?

**Answer, in one line:** none of the tested settings does. Fills that look 1–2.5 vol points profitable at the trade are about 1 vol point unprofitable an hour later, delta-hedged. The settings that help are the ones that trade less: a wider spread and a wider hedge band.

Read the [research note](research_note.md), or see the full [results table](phase8_results.md).

## Run it

```bash
uv sync                                    # Python 3.13, dependencies
uv run pytest                              # 568 tests
uv run python -m mm.data.cli download --config config/base.yaml   # once: Deribit history API → data/raw
uv run python -m mm.data.cli clean    --config config/base.yaml   # → data/clean

# every result: 14 runs (train-period selection, test-period report), table and figures
uv run python -m mm.run --config config/base.yaml --experiments config/experiments.yaml
```

Data is downloaded from Deribit's free public history API and cached locally under `data/`; it is not committed. Runs are cached by a hash of their resolved config, so an interrupted batch resumes where it stopped.

A single run and its reports:

```bash
uv run python -m mm.run --config config/base.yaml --start 2025-01-06 --end 2025-01-13
uv run python -m mm.pnl.report   --config config/base.yaml --run 2025-01-06_2025-01-13   # attribution, markouts
uv run python -m mm.sim.handcheck --config config/base.yaml --run 2025-01-06_2025-01-13  # 20 fills recomputed
```

## Where the results are

| file | what |
|---|---|
| [`reports/research_note.md`](research_note.md) | the write-up: setup, simplifications, results, the three charts, what was learned |
| [`reports/phase8_results.md`](phase8_results.md) | full results table, P&L by component, train selection, one mechanism per experiment |
| [`reports/decisions.md`](decisions.md) | every modelling choice (D001–D046): the choice, the alternative, why, and its category |
| [`reports/extensions.md`](extensions.md) | ideas outside the scope |
| `reports/phase1_data.md`, `phase2_validation.md`, `phase3_surface.md` | data checks, pricing validation against Deribit IVs, surface fit quality |
| `reports/pnl_*.md`, `sim_*.md`, `*handcheck*.md` | single-run reports from development |

## Layout

```
config/base.yaml          all parameters (nothing tunable is hard-coded)
config/experiments.yaml   the Phase 8 grid
src/mm/
  data/         downloaders (paging, retries, rate limits), cleaning, instrument parsing
  pricing/      Black-76 (undiscounted, coin-settled), Greeks, IV solver, forward curve
  surface/      eSSVI fits per expiry, arbitrage checks, summary statistics
  quoting/      fair value, vol spread with price floor, inventory skew, sizing, universe
  sim/          event loop, strict fill model, fees, funding, expiry settlement, accounting
  hedging/      delta-band hedger on the inverse perpetual
  risk/         Greek aggregation, vega / gamma / position limits
  pnl/          waterfall attribution, account replay, markouts
  experiments/  runner (train → select → test), metrics, results table, note figures
  run.py        single entry point
tests/          unit, property-based (hypothesis) and synthetic end-to-end tests
reports/        write-up, results, decisions, figures
```

## Main design points

- **No lookahead.** Quotes use only information timestamped before trade time − latency. Hedges fill at the first same-side perp print at or after decision + latency. Tests fail otherwise.
- **Strict fills.** A trade fills our quote only if it prints *through* it, for 50% of the smaller size, at our price, paying the maker fee.
- **Valuation at the market's surface.** P&L is always measured at Surface A, never at a variant's own quotes.
- **P&L reconciles.** An independent replay of the account matches the engine, and the components (theta, delta, gamma, basis, vega, spread capture, hedge cost, fees, funding, settlement) sum to the total with a residual of about 1e-9 USD.
- **Train / test split.** Parameters and the stress threshold are chosen on the first 65% of days. Results are reported on the rest as paired daily differences against the baseline.
