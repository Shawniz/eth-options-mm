# ETH Options Market-Making Simulator

A market maker for ETH options on Deribit, simulated against **real historical trades**. It quotes around a fitted vol surface, delta-hedges with the perpetual, enforces Greek limits and attributes every dollar of P&L.

**Question:** which quoting and risk choices make money after adverse selection, hedging costs and fees?

**Answer:** none of the settings tested, on an out-of-sample test period of 63 days (29 Apr – 30 Jun 2025).

- Fills enter with +2.4 vol points of edge. One hour later, delta-hedged, they are about −1 vol point: the flow is informed.
- What helps is trading less. A wider spread adds +$991 ± 603 per day and a wider hedge band +$348 ± 102. The best setting cuts the loss from −$189k to −$111k.

**→ [Research note](reports/research_note.md)** · [Results](reports/phase8_results.md) · [Decisions](reports/decisions.md) · [Project guide](reports/project_guide.md)

## Run

```bash
uv sync && uv run pytest
uv run python -m mm.data.cli download --config config/base.yaml && uv run python -m mm.data.cli clean --config config/base.yaml
uv run python -m mm.run --config config/base.yaml --experiments config/experiments.yaml   # every result and figure
```

Python 3.13 · numpy · scipy · pandas · duckdb · pydantic · matplotlib. Data comes from Deribit's public history API and is cached locally, not committed.
