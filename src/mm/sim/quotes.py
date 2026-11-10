"""Phase 4 placeholder quoter: fair vol +- a flat half-spread, priced and rounded.

Bid rounded DOWN and ask UP to the tick (the tick depends on the price level). The
quote is always at least one tick wide. A bid below the smallest tick is not posted.
Phase 5 replaces this with the full quoter (fee floor, skew, sizing, universe).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from mm.config import FixedQuoteConfig, OptionSpecs
from mm.pricing.black76 import VOL_POINT, price_eth

EPS = 1e-9


@dataclass(frozen=True)
class Quote:
    bid: float | None  # ETH, None = no bid
    ask: float
    size: float  # contracts per side


def round_bid(p: float, spec: OptionSpecs) -> float:
    tick = spec.tick_for(p)
    return round(math.floor(p / tick + EPS) * tick, 10)


def round_ask(p: float, spec: OptionSpecs) -> float:
    tick = spec.tick_for(p)
    return round(math.ceil(p / tick - EPS) * tick, 10)


def fixed_quote(
    sigma: float,
    f: float,
    k: float,
    t: float,
    is_call: bool,
    qcfg: FixedQuoteConfig,
    spec: OptionSpecs,
) -> Quote:
    h = qcfg.half_spread_volpts * VOL_POINT
    min_tick = spec.tick_steps[0].tick_eth
    bid = None
    if sigma - h > 0:
        b = round_bid(float(price_eth(f, k, t, sigma - h, is_call)), spec)
        bid = b if b >= min_tick - EPS else None
    ask = max(round_ask(float(price_eth(f, k, t, sigma + h, is_call)), spec), min_tick)
    if bid is not None and ask - bid < spec.tick_for(bid) - EPS:
        ask = round(bid + spec.tick_for(bid), 10)
    return Quote(bid, ask, qcfg.size_contracts)
