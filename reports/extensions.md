# Extensions

Ideas that are outside the current scope. They go here, not into the code. Each one names the phase where it came up and why it was deferred.

## From CLAUDE.md (agreed, do not build unless asked)
- **Surface B:** a surface fitted to trade IVs, compared with Surface A as a quoting input.
- **Spread shape:** a calibrated, vega-based spread fitted to effective spreads, with regime-dependent widening.
- **Risk:** vega buckets by tenor, and a kill switch.
- **Hedging:** a comparison of hedge policies (fixed interval, Whalley–Wilmott band), smile-adjusted delta, and dated-future hedges for long expiries.
- **Books:** an ETH-denominated book, and BTC as a second underlying.
- **Signal:** Aave liquidation pressure as an input to skew and spread.

## Raised during the build
- **Exchange pauses (Phase 1):** a simulator "pause" mode that drops quotes and forces a surface refit after an exchange-wide pause. H1 2025 had 7 maintenance-like pauses at 09:00 UTC and 1 incident. Build it in Phase 4 if cheap; otherwise state it as a simplification.
- **Delivery fee (Phase 1):** confirm the option delivery fee on Deribit's fee page (D012).
