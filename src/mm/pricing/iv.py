"""Implied vol from ETH premiums, vectorised and safeguarded.

Method: solve on the OUT-of-the-money side (call if K >= F, else put; the other is
converted with ETH put-call parity c - p = 1 - K/F). The OTM price is pure time value,
so there is no precision loss from subtracting a large intrinsic value. Then Newton
steps inside a bracket [lo, hi] that shrinks every iteration; any Newton step that
leaves the bracket is replaced by bisection, so the solver cannot diverge.

Every row gets a Status. Rows that are not OK get NaN, never a guessed vol.
"""

from __future__ import annotations

from enum import IntEnum

import numpy as np

from mm.pricing.black76 import VOL_POINT, price_eth, vega

VOL_LO, VOL_HI = 1e-4, 10.0  # bracket: 0.01% to 1000% vol
MAX_ITER = 100
PRICE_TOL = 1e-14  # ETH; Deribit's tick is 1e-4


class Status(IntEnum):
    OK = 0
    BELOW_INTRINSIC = 1  # price < intrinsic value: no vol fits
    ABOVE_MAX = 2  # price >= the no-arbitrage upper bound
    NO_TIME_VALUE = 3  # price == intrinsic: vol undefined (any small vol fits)
    NOT_CONVERGED = 4
    BAD_INPUT = 5  # T <= 0, F <= 0, K <= 0 or NaN price


def implied_vol(price, f, k, t, is_call):
    """Returns (iv, status) arrays broadcast to the input shape."""
    price, f, k, t, is_call = np.broadcast_arrays(
        np.asarray(price, float),
        np.asarray(f, float),
        np.asarray(k, float),
        np.asarray(t, float),
        np.asarray(is_call, bool),
    )
    shape = price.shape
    price, f, k, t, is_call = (a.ravel() for a in (price, f, k, t, is_call))
    status = np.full(price.shape, Status.OK, dtype=np.int8)
    iv = np.full(price.shape, np.nan)

    bad = ~((t > 0) & (f > 0) & (k > 0) & np.isfinite(price))
    status[bad] = Status.BAD_INPUT
    with np.errstate(invalid="ignore", divide="ignore"):
        kf = k / f
        intrinsic = np.where(is_call, np.maximum(1 - kf, 0), np.maximum(kf - 1, 0))
        upper = np.where(is_call, 1.0, kf)
        tv = price - intrinsic  # time value (ETH)
    live = ~bad
    status[live & (tv < -PRICE_TOL)] = Status.BELOW_INTRINSIC
    status[live & (price >= upper)] = Status.ABOVE_MAX
    status[live & (np.abs(tv) <= PRICE_TOL) & (status == Status.OK)] = Status.NO_TIME_VALUE

    todo = status == Status.OK
    if todo.any():
        iv[todo], conv = _solve(tv[todo], f[todo], k[todo], t[todo])
        idx = np.flatnonzero(todo)
        status[idx[~conv]] = Status.NOT_CONVERGED
        iv[idx[~conv]] = np.nan
    return iv.reshape(shape), status.reshape(shape)


def _solve(tv, f, k, t):
    """Vol such that the OTM option's ETH price equals the time value tv."""
    otm_call = k >= f
    lo = np.full(tv.shape, VOL_LO)
    hi = np.full(tv.shape, VOL_HI)
    # If even VOL_HI is too cheap, there is no solution inside the bracket.
    s = np.full(tv.shape, 0.5)
    conv = np.zeros(tv.shape, bool)
    for _ in range(MAX_ITER):
        g = price_eth(f, k, t, s, otm_call) - tv
        conv = np.abs(g) <= PRICE_TOL + 1e-12 * tv
        if conv.all():
            break
        lo = np.where(g < 0, s, lo)
        hi = np.where(g > 0, s, hi)
        v = np.asarray(vega(f, k, t, s)) / VOL_POINT / f  # dPrice_eth / dsigma
        with np.errstate(invalid="ignore", divide="ignore"):
            newton = s - g / v
        inside = np.isfinite(newton) & (newton > lo) & (newton < hi)
        s = np.where(conv, s, np.where(inside, newton, 0.5 * (lo + hi)))
    return s, conv


def vol_for_price_change(price, d_price, f, k, t, sigma, is_call):
    """Vol shift d_sigma such that P(sigma + d_sigma) = P(sigma) + d_price, by exact
    repricing (CLAUDE.md), not d_price / vega. `price` must equal P(sigma)."""
    target, status = implied_vol(np.asarray(price) + np.asarray(d_price), f, k, t, is_call)
    out = np.where(status == Status.OK, target - np.asarray(sigma, float), np.nan)
    return float(out) if np.ndim(out) == 0 else out
