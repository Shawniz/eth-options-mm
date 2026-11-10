"""Vega, cash-gamma and per-instrument position limits (CLAUDE.md decision 10; D035, D043).

Each side's quote is cut to the room left under both limits, so a fill (never larger
than the quote) cannot breach a limit. Near a limit the risk-increasing side shrinks and
then disappears (below the minimum order); once drift breaches a limit, only the
risk-reducing side has room.
"""

from __future__ import annotations

import math

from mm.config import RiskConfig


def side_room(net_vega_usd: float, vega_i: float, pos: float, r: RiskConfig,
              net_gamma_usd: float = 0.0, gamma_i: float = 0.0):  # fmt: skip
    """Contracts we may still BUY (bid) and SELL (ask) on this instrument. Gamma is cash
    gamma (USD per 1% move squared): buying adds +gamma_i, selling -gamma_i."""
    cap = r.max_position_contracts
    bid, ask = cap - pos, cap + pos
    lim = r.vega_limit_usd
    if vega_i > 0 and math.isfinite(vega_i):
        bid = min(bid, (lim - net_vega_usd) / vega_i)
        ask = min(ask, (lim + net_vega_usd) / vega_i)
    g = r.gamma_limit_usd
    if gamma_i > 0 and math.isfinite(gamma_i):
        bid = min(bid, (g - net_gamma_usd) / gamma_i)
        ask = min(ask, (g + net_gamma_usd) / gamma_i)
    return bid, ask
