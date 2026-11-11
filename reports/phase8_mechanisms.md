## Mechanisms (one sentence per experiment)

Markouts are delta-hedged and in vol points per unit of filled vega (positive = good for us).
Component totals are from the table above.

- **Headline.** No variant makes money on the test period. Every fill is entered with
  1.1–2.4 vol points of edge, but by 1 hour the delta-hedged markout is −0.4 to −1.1 vol
  points: the takers who trade with us are informed about the next hour's move. Spread
  capture ($41–168k) is then outweighed by short-gamma losses ($58–185k) plus hedging
  costs and fees ($30–141k).
- **Spread (2 > 1 > 0.5; train and test agree).** Doubling the half-spread halves the
  fills (13,360 → 7,740) but keeps 88% of the spread capture, because the edge per fill is
  bigger (1.49 → 2.43 vol points). The smaller inventory cuts the gamma loss by $44k and
  hedging and fees by $37k: +$991 ± 603 per day, most of it on stress days. Fills at a
  wide spread are more toxic per unit of vega (1 h markout −1.06 against −0.85), so the
  gain comes from trading less, not from better trades. Narrowing to 0.5 does the
  opposite (−$478 ± 443 per day).
- **Skew (off ≈ on; test difference −$1 ± 564 per day).** Skew changes which risk the
  book carries, not what it earns. With κ = 1 the book stays short options (theta +$18k,
  gamma −$166k, mean |vega| 18% of the limit). With κ = 0 it drifts long vega (48% of the
  limit), pays theta (−$25k) and loses less on gamma (−$129k). The two offset. Skew-off was
  chosen on train by $336 per day, which did not carry over to test.
- **Hedge band (10 > 5 ETH).** The fills are identical. Hedges fall from 7,846 to 2,534,
  saving $30k of taker fees and spread, while the extra unhedged delta adds only noise
  (gamma −$5k, delta and basis −$3k): +$348 ± 102 per day. This is the most precise
  result. Its lag-1 autocorrelation of 0.38 means the error bar is about 1.5× too narrow
  (≈ ±150), but it is still clearly positive.
- **Latency (1 s vs 0).** A 1-second delay makes quotes slightly stale. The book is
  picked off on underlying moves (delta and basis +$16k → −$4k), and the edge at fill
  drops from 1.49 to 1.40 vol points. The cost is −$153 ± 172 per day, not distinguishable
  from zero over 63 days. At these spreads, one second of latency matters less than the
  spread and hedging choices.
- **Best (spread 2, skew off, band 10).** The gains roughly add up: +$1,241 ± 908 per day,
  against +$1,339 for spread and band separately (skew adds nothing). Hedges drop to
  1,439. The loss halves to −$111k but is still a loss.
- **Robustness (fill model).** The loss scales with traded volume, so the conclusion
  "these settings lose money" holds under every fill model.
  - With stricter fills (25% of the trade) the loss falls to −$59k.
  - With looser fills (100%, plus half-fills at our price) it grows to −$199k.
    Spread capture more than doubles to $168k, but gamma, theta and hedging grow with it.
    The 1 h markout improves (−0.42), because the at-price fills are less informed.
  - Their "Δ vs base" column compares against the baseline under the default fills, so it
    measures the fill model, not the setting. The baseline was not rerun under the other
    fill models (lean grid, D042), so whether "best" still beats it under looser fills is
    untested.

**Caveat on calm vs stress.** Stress has 23 of the 63 test days, so the per-regime error
bars are wide. Only the band result is significant in both regimes.
