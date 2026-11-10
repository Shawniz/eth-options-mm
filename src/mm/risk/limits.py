"""Vega and per-instrument position limits (CLAUDE.md decision 10; D035).

Each side's quote is cut to the room left under both limits, so a fill (never larger
than the quote) cannot breach a limit. Near a limit the risk-increasing side shrinks and
then disappears (below the minimum order); once drift breaches a limit, only the
risk-reducing side has room.
"""

from __future__ import annotations

import math

from mm.config import RiskConfig


def side_room(net_vega_usd: float, vega_i: float, pos: float, r: RiskConfig):
    """Contracts we may still BUY (bid) and SELL (ask) on this instrument."""
    cap = r.max_position_contracts
    bid, ask = cap - pos, cap + pos
    lim = r.vega_limit_usd
    if vega_i > 0 and math.isfinite(vega_i):
        bid = min(bid, (lim - net_vega_usd) / vega_i)
        ask = min(ask, (lim + net_vega_usd) / vega_i)
    return bid, ask
