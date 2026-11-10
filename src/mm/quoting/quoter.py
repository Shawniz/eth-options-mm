"""Phase 5 quoter: fair vol from Surface A, flat vol spread, price floor, skew, sizing.

For one instrument at one trade (all inputs latency-shifted):
    universe   min_days <= T <= max_days and OTM delta >= min_abs_delta, where
               OTM delta = min(|call delta|, |put delta|) at fair vol (D030)
    skew       dsigma = clip(-kappa * net_vega / vega_limit, -h, +h)   (cap = h, D032, D035)
               sigma_c = sigma_fair + dsigma: long vega -> both quotes lower
    spread     bid_raw = P(sigma_c - h), ask_raw = P(sigma_c + h)
    floor      half_min = max(min_half_width_ticks x tick(P_c), fee_cover x fee(P_c))
               bid = round_down(min(bid_raw, P_c - half_min))
               ask = round_up(max(ask_raw, P_c + half_min))          (D031)
    size       vega: floor(min(target_vega / vega_i, max_contracts)) to the order
               increment; fixed: fixed_contracts. No quote below the minimum order (D033)
    limits     each side cut to the room under the vega and position limits; a side
               below the minimum order is dropped (risk.limits, D035)
P(.) is the ETH premium C/F (Black-76, undiscounted). vega_i is USD per vol point.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from mm.config import OptionSpecs, QuotingConfig, UniverseConfig
from mm.pricing.black76 import VOL_POINT, price_eth, vega
from mm.sim.fees import option_fee_eth

EPS = 1e-9


@dataclass(frozen=True)
class Quote:
    bid: float | None  # ETH, None = no bid
    ask: float | None  # None only when a limit removed the side
    size: float  # contracts per side before limits
    sigma_center: float
    skew_volpts: float
    skew_capped: bool
    floor_bound: bool  # the price floor, not the vol spread, set at least one side
    bid_size: float = 0.0  # after limits (0 = side not quoted)
    ask_size: float = 0.0


def round_bid(p: float, spec: OptionSpecs) -> float:
    tick = spec.tick_for(p)
    return round(math.floor(p / tick + EPS) * tick, 10)


def round_ask(p: float, spec: OptionSpecs) -> float:
    tick = spec.tick_for(p)
    return round(math.ceil(p / tick - EPS) * tick, 10)


def otm_delta(f, k, t, sigma):
    """min(|call delta|, |put delta|) = min(N(d1), 1 - N(d1)). Vectorised."""
    from scipy.special import ndtr

    f, k, t, s = (np.asarray(v, dtype=float) for v in (f, k, t, sigma))
    with np.errstate(invalid="ignore", divide="ignore"):
        sd = s * np.sqrt(t)
        n1 = ndtr((np.log(f / k) + 0.5 * sd * sd) / sd)
    out = np.minimum(n1, 1.0 - n1)
    return float(out) if out.ndim == 0 else out


def in_universe(otm: float, days: float, u: UniverseConfig) -> bool:
    return bool(u.min_days <= days <= u.max_days and otm >= u.min_abs_delta)


def skew_volpts(net_vega_usd: float, q: QuotingConfig, vega_ref_usd: float) -> tuple[float, bool]:
    """Centre shift in vol points, capped at the half-spread: never quote through fair.
    vega_ref_usd is the vega limit (D035): kappa is the skew at the limit."""
    raw = -q.kappa_volpts * net_vega_usd / vega_ref_usd
    cap = q.half_spread_volpts
    return float(min(max(raw, -cap), cap)), abs(raw) > cap + EPS


def round_size(raw: float, spec: OptionSpecs) -> float | None:
    """Down to the order increment; None below the minimum order."""
    if not raw > 0:
        return None
    inc = spec.min_order_contracts
    size = round(math.floor(raw / inc + EPS) * inc, 10)
    return size if size >= spec.min_order_contracts - EPS else None


def quote_size(vega_usd: float, q: QuotingConfig, spec: OptionSpecs) -> float | None:
    if q.sizing == "fixed":
        return round_size(q.fixed_contracts, spec)
    if not vega_usd > 0:
        return None
    return round_size(min(q.target_vega_usd / vega_usd, q.max_contracts), spec)


def apply_limits(qt: Quote, bid_room: float, ask_room: float, spec: OptionSpecs) -> Quote:
    """Cut each side to the room left under the limits; drop a side below the minimum."""
    bs = round_size(min(qt.size, bid_room), spec) if qt.bid is not None else None
    asz = round_size(min(qt.size, ask_room), spec) if qt.ask is not None else None
    return replace(
        qt,
        bid=qt.bid if bs else None,
        ask=qt.ask if asz else None,
        bid_size=bs or 0.0,
        ask_size=asz or 0.0,
    )


def make_quote(
    sigma_fair: float,
    f: float,
    k: float,
    t: float,
    is_call: bool,
    net_vega_usd: float,
    q: QuotingConfig,
    spec: OptionSpecs,
    vega_ref_usd: float,
    index_usd: float | None = None,
) -> Quote | None:
    """Two-sided quote before limits, or None (no size, or no valid centre vol).
    vega_ref_usd: the vega limit (skew scale). index_usd: spot, so vega_i = Black vega x
    S/F (USD at spot); omitted -> S = F."""
    size = quote_size(float(vega(f, k, t, sigma_fair)) * ((index_usd or f) / f), q, spec)
    if size is None:
        return None
    shift, capped = skew_volpts(net_vega_usd, q, vega_ref_usd)
    sc = sigma_fair + shift * VOL_POINT
    h = q.half_spread_volpts * VOL_POINT
    if not sc > 0:
        return None
    pc = float(price_eth(f, k, t, sc, is_call))
    half_min = max(
        q.min_half_width_ticks * spec.tick_for(pc),
        q.fee_cover * option_fee_eth(1.0, pc, spec, maker=True),
    )
    min_tick = spec.tick_steps[0].tick_eth
    ask_raw = float(price_eth(f, k, t, sc + h, is_call))
    ask_target = max(ask_raw, pc + half_min)
    floor_bound = ask_target > ask_raw + EPS * min_tick
    ask = max(round_ask(ask_target, spec), min_tick)
    bid = None
    bid_raw = float(price_eth(f, k, t, sc - h, is_call)) if sc - h > 0 else -math.inf
    bid_target = min(bid_raw, pc - half_min)
    floor_bound |= bid_target < bid_raw - EPS * min_tick
    if bid_target >= min_tick - EPS:
        b = round_bid(bid_target, spec)
        bid = b if b >= min_tick - EPS else None
    if bid is not None and ask - bid < spec.tick_for(bid) - EPS:
        ask = round(bid + spec.tick_for(bid), 10)
    return Quote(bid, ask, size, sc, shift, capped, floor_bound, size if bid else 0.0, size)
