"""Undiscounted Black-76 on the forward, with Deribit's coin-settled premium.

    C = F N(d1) - K N(d2),  P = K N(-d2) - F N(-d1)
    d1 = [ln(F/K) + sigma^2 T / 2] / (sigma sqrt T),  d2 = d1 - sigma sqrt T
    ETH premium = USD price / F

Greeks follow Deribit's displayed conventions (verified against live tickers):
    delta  N(d1) for calls, N(d1) - 1 for puts   (Black delta, NOT premium-adjusted;
           the premium effect comes through the ETH balance in the account delta)
    gamma  per USD of forward
    vega   USD per 1 vol point (0.01)
    theta  USD per calendar day (ACT/365)

All functions broadcast over numpy arrays; scalar inputs return a Python float.
Invalid inputs (T <= 0, sigma <= 0, F <= 0, K <= 0) return NaN.
"""

from __future__ import annotations

import numpy as np
from scipy.special import ndtr

_SQRT_2PI = np.sqrt(2.0 * np.pi)
DAYS_PER_YEAR = 365.0
VOL_POINT = 0.01


def _pdf(x):
    return np.exp(-0.5 * x * x) / _SQRT_2PI


def _out(x):
    return float(x) if np.ndim(x) == 0 else x


def _prep(f, k, t, s):
    f, k, t, s = (np.asarray(v, dtype=float) for v in (f, k, t, s))
    bad = ~((t > 0) & (s > 0) & (f > 0) & (k > 0))
    with np.errstate(invalid="ignore", divide="ignore"):
        sd = s * np.sqrt(np.where(bad, np.nan, t))
        d1 = (np.log(f / k) + 0.5 * sd * sd) / sd
    return f, k, t, s, d1, d1 - sd, bad


def price_usd(f, k, t, sigma, is_call):
    f, k, t, s, d1, d2, bad = _prep(f, k, t, sigma)
    call = f * ndtr(d1) - k * ndtr(d2)
    put = k * ndtr(-d2) - f * ndtr(-d1)
    return _out(np.where(bad, np.nan, np.where(is_call, call, put)))


def price_eth(f, k, t, sigma, is_call):
    """Deribit premium in ETH = USD price / F."""
    f_arr = np.asarray(f, dtype=float)
    return _out(np.asarray(price_usd(f, k, t, sigma, is_call)) / f_arr)


def delta(f, k, t, sigma, is_call):
    *_, d1, _, bad = _prep(f, k, t, sigma)
    d = np.where(is_call, ndtr(d1), ndtr(d1) - 1.0)
    return _out(np.where(bad, np.nan, d))


def gamma(f, k, t, sigma):
    f, k, t, s, d1, _, bad = _prep(f, k, t, sigma)
    with np.errstate(invalid="ignore", divide="ignore"):
        g = _pdf(d1) / (f * s * np.sqrt(t))
    return _out(np.where(bad, np.nan, g))


def vega(f, k, t, sigma):
    """USD per 1 vol point."""
    f, k, t, s, d1, _, bad = _prep(f, k, t, sigma)
    with np.errstate(invalid="ignore"):
        v = f * _pdf(d1) * np.sqrt(t) * VOL_POINT
    return _out(np.where(bad, np.nan, v))


def theta(f, k, t, sigma):
    """USD per calendar day. Same for calls and puts (r = 0, undiscounted)."""
    f, k, t, s, d1, _, bad = _prep(f, k, t, sigma)
    with np.errstate(invalid="ignore", divide="ignore"):
        th = -f * _pdf(d1) * s / (2.0 * np.sqrt(t)) / DAYS_PER_YEAR
    return _out(np.where(bad, np.nan, th))
