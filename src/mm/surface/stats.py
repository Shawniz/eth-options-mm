"""Smile summary statistics: ATM vol, 25-delta risk reversal and butterfly.

25-delta strikes are found by root-finding Black delta with the smile's OWN vol at that
strike (CLAUDE.md): solve delta(k, sigma(k)) = +0.25 (call) or -0.25 (put).
A smile is either an SVI or any callable vol(k).
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
from scipy.optimize import brentq

from mm.pricing.black76 import delta
from mm.surface.svi import SVI, svi_vol


def _volfn(smile, t: float) -> Callable:
    if isinstance(smile, SVI):
        return lambda k: svi_vol(smile, k, t)
    return smile


def _scalar(v) -> float:
    return float(np.asarray(v).ravel()[0])


def atm_vol(smile, t: float) -> float:
    return _scalar(_volfn(smile, t)(0.0))


def strike_for_delta(smile, t: float, target: float, is_call: bool) -> float:
    """Log-moneyness k with Black delta(k, sigma(k)) = target."""
    vol = _volfn(smile, t)

    def f(k):
        return delta(1.0, np.exp(k), t, _scalar(vol(k)), is_call) - target

    width = 5.0 * max(atm_vol(smile, t), 0.05) * np.sqrt(t)
    lo, hi = -width, width
    for _ in range(6):  # widen until the root is bracketed
        if f(lo) * f(hi) < 0:
            break
        lo, hi = 2 * lo, 2 * hi
    return brentq(f, lo, hi, xtol=1e-14, rtol=1e-14, maxiter=200)


def rr25(smile, t: float) -> float:
    vol = _volfn(smile, t)
    kc = strike_for_delta(smile, t, 0.25, True)
    kp = strike_for_delta(smile, t, -0.25, False)
    return _scalar(vol(kc)) - _scalar(vol(kp))


def bf25(smile, t: float) -> float:
    vol = _volfn(smile, t)
    kc = strike_for_delta(smile, t, 0.25, True)
    kp = strike_for_delta(smile, t, -0.25, False)
    return 0.5 * (_scalar(vol(kc)) + _scalar(vol(kp))) - atm_vol(smile, t)


def smile_stats(smile, t: float) -> dict:
    vol = _volfn(smile, t)
    kc = strike_for_delta(smile, t, 0.25, True)
    kp = strike_for_delta(smile, t, -0.25, False)
    atm, vc, vp = atm_vol(smile, t), _scalar(vol(kc)), _scalar(vol(kp))
    return {"atm": atm, "rr25": vc - vp, "bf25": 0.5 * (vc + vp) - atm, "k25c": kc, "k25p": kp}
