# Extensions

Ideas that are outside the current scope. They go here, not into the code. Each one names the phase where it came up and why it was deferred.

## From CLAUDE.md (agreed, do not build unless asked)
- **Surface B:** a surface fitted to trade IVs, compared with Surface A as a quoting input.
- **Spread shape:** a calibrated, vega-based spread fitted to effective spreads, with regime-dependent widening.
- **Risk:** vega buckets by tenor, and a kill switch.
- **Hedging:** a comparison of hedge policies (fixed interval, Whalley–Wilmott band), and dated-future hedges for long expiries. (Smile-adjusted delta was built in Phase 6, D038.)
- **Books:** an ETH-denominated book, and BTC as a second underlying.
- **Signal:** Aave liquidation pressure as an input to skew and spread.

## Raised during the build
- **Exchange pauses (Phase 1):** a simulator "pause" mode that drops quotes and forces a surface refit after an exchange-wide pause. H1 2025 had 7 maintenance-like pauses at 09:00 UTC and 1 incident. Build it in Phase 4 if cheap; otherwise state it as a simplification.
- **Delivery fee (Phase 1):** confirm whether daily options are exempt from the delivery fee (D012). The rate and cap are confirmed.
- **Risk-reversal / vega-bucket limits (Phase 6):** the total-vega limit nets put vega against call vega. On 2 Feb 2025 the book held +$980 of put vega against −$628 of call vega, for a net $352 but a gross $3,474. A gross-vega or bucketed limit would cap that.
- **Faster refits in fast markets (Phase 6):** on 3 Feb 2025 we traded 7,716 contracts (half the fortnight) against a surface refitted every 5 minutes from a 60-minute window. The 1-hour delta-hedged markout that day was −$77k against +$20k of edge at fill (D037). An event-triggered refit (e.g. on a large index move), or a spread that widens with realised index moves, would cut the staleness.
- **Margin (Phase 6):** the ETH balance went negative (−9.5 ETH on 7 Feb 2025) while equity stayed positive. Deribit's margin is not modelled; a margin check or liquidation rule would be needed for a live-like account.
- **Speed (Phase 6):** about 50 s per simulated week, mostly delta checks at about 80k perp prints a day. A minimum check interval or a gamma-based skip rule would speed this up if Phase 8 needs it.
