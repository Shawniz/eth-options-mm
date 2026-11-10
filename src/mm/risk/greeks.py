"""Book Greek aggregation (USD). Phase 5: vega for inventory skew; Phase 6 extends."""

from __future__ import annotations

import numpy as np

from mm.pricing.black76 import gamma, vega


def book_vega_usd(n, f, k, t, sigma, s) -> float:
    """sum n_i x Black vega_i x S/F: USD per 1 vol point of account value
    (an option is worth n (C/F) S in USD, so dW/dsigma = n (S/F) dC/dsigma).
    Positions with no valid inputs contribute 0."""
    n, f, s = (np.asarray(v, dtype=float) for v in (n, f, s))
    v = np.asarray(vega(f, k, t, sigma), dtype=float) * s / f
    return float(np.nansum(n * v))


def gamma1_usd(f, k, t, sigma, s):
    """Cash gamma per contract: USD gamma P&L of a 1% move in ETH, 1/2 G_S (0.01 S)^2.
    The account holds n (C/F) S with F = b S, so d2W/dS2 = n b Gamma_Black(F)."""
    f, s = np.asarray(f, dtype=float), np.asarray(s, dtype=float)
    return 0.5 * (f / s) * np.asarray(gamma(f, k, t, sigma), dtype=float) * (0.01 * s) ** 2


def book_gamma1_usd(n, f, k, t, sigma, s) -> float:
    """sum n_i x cash gamma_i (USD per 1% move squared); unvalued positions count 0."""
    return float(np.nansum(np.asarray(n, dtype=float) * gamma1_usd(f, k, t, sigma, s)))


def perp_value_eth(q_usd: float, p0: float, p: float) -> float:
    """Open inverse-perp P&L in ETH, Q (1/P0 - 1/P)."""
    return 0.0 if q_usd == 0 else q_usd * (1.0 / p0 - 1.0 / p)


def account_delta_eth(acct, opt_deltas: dict[str, float]) -> float:
    """delta = B + sum n_i x delta_i + Q / P0 (ETH, i.e. USD per $1 in ETH; CLAUDE.md).
    Premiums already sit in B, so option deltas are NOT premium-adjusted."""
    d = acct.eth + sum(n * opt_deltas[name] for name, n in acct.positions.items())
    return d + (acct.perp_usd / acct.perp_entry if acct.perp_usd else 0.0)


def usd_value(acct, s_usd: float, perp_px: float, opt_eth: float = 0.0) -> float:
    """W = S (B + sum n_i C_i/F_i + Q (1/P0 - 1/P)); opt_eth = sum n_i C_i/F_i."""
    return s_usd * (acct.eth + opt_eth + perp_value_eth(acct.perp_usd, acct.perp_entry, perp_px))
