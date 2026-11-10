"""Delta-band hedger with the inverse perpetual (CLAUDE.md decision 9; D034, D036).

Account delta (ETH = USD per $1 move in ETH):  delta = B + sum n_i N(d1_i) + Q / P0
Trigger: |delta| > delta_band. Hedge back to zero: dQ = -S x delta (USD notional),
rounded to whole contracts. Execution: taker, at the first SAME-SIDE perp print strictly
after the trigger with ts >= trigger + latency (a sell hedge gets a taker-sell print, i.e.
the bid), times (1 +- hedge_cost_bps). Taker fee = rate x |dQ| / price, in ETH.
Inverse perp P&L in ETH: Q (1/P0 - 1/P).
"""

from __future__ import annotations

from mm.config import PerpSpecs


def hedge_qty(delta_eth: float, s_usd: float, contract_usd: float) -> float:
    """USD notional to trade (buy > 0) that takes the account delta to ~0."""
    return round(-s_usd * delta_eth / contract_usd) * contract_usd


def needs_hedge(delta_eth: float, band_eth: float) -> bool:
    return abs(delta_eth) > band_eth


def exec_price(print_usd: float, sign: int, bps: float) -> float:
    """Buy (sign +1) pays more, sell (-1) receives less than the print."""
    return print_usd * (1.0 + sign * bps / 1e4)


def perp_fee_eth(q_usd: float, price_usd: float, spec: PerpSpecs, taker: bool = True) -> float:
    rate = spec.taker_fee_rate if taker else spec.maker_fee_rate
    return abs(q_usd) * rate / price_usd


def perp_pnl_eth(q_usd: float, p0: float, p: float) -> float:
    """Inverse perpetual P&L in ETH for USD notional q entered at p0, now at p."""
    return 0.0 if q_usd == 0 else q_usd * (1.0 / p0 - 1.0 / p)
