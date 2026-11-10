"""Fees, expiry payoff and funding, as pure functions (all amounts in ETH).

Option trading fee (per contract): min(rate, cap x premium). Option fills are maker.
Delivery fee (per contract, options that finish in the money): min(rate, cap x payoff);
daily options exempt if configured (D012, unverified).
Funding on the inverse perpetual (USD notional Q, long > 0): the long pays
Q x rate / index in ETH when the rate is positive.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mm.config import OptionSpecs

FRIDAY = 4


def option_fee_eth(qty: float, price_eth: float, spec: OptionSpecs, maker: bool = True) -> float:
    rate = spec.maker_fee_eth_per_contract if maker else spec.taker_fee_eth_per_contract
    return abs(qty) * min(rate, spec.fee_cap_fraction_of_premium * price_eth)


def payoff_eth(strike_usd: float, is_call: bool, delivery_usd: float) -> float:
    """Value per contract at expiry, in ETH (cash-settled at the delivery price)."""
    intrinsic = delivery_usd - strike_usd if is_call else strike_usd - delivery_usd
    return max(intrinsic, 0.0) / delivery_usd


def is_daily_expiry(expiry_ms: int) -> bool:
    """Weekly and longer expiries are Fridays; any other weekday is a daily option."""
    return datetime.fromtimestamp(expiry_ms / 1000, UTC).weekday() != FRIDAY


def delivery_fee_eth(qty: float, payoff_per_contract: float, spec: OptionSpecs, daily: bool):
    if payoff_per_contract <= 0 or (daily and spec.delivery_fee_exempt_daily):
        return 0.0
    rate = spec.delivery_fee_eth_per_contract
    return abs(qty) * min(rate, spec.delivery_fee_cap_fraction * payoff_per_contract)


def funding_eth(perp_usd: float, interest: float, index_usd: float) -> float:
    """ETH paid (< 0) or received (> 0) over one funding interval."""
    return -perp_usd * interest / index_usd
