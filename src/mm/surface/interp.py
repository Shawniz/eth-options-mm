"""Interpolation across expiries, linear in total variance at fixed k.

Between two fitted expiries: w(k, T) = w1(k) + (T - T1) / (T2 - T1) * (w2(k) - w1(k)).
Outside the fitted range the vol is held flat (w scales with T). Linear interpolation
in w preserves the calendar condition whenever the nodes satisfy it.
"""

from __future__ import annotations

import numpy as np

from mm.surface.svi import SVI, svi_w


def total_variance_interp(nodes: list[tuple[float, SVI]], k, t: float) -> np.ndarray:
    nodes = sorted(nodes, key=lambda n: n[0])
    if not nodes:
        raise ValueError("no fitted expiries to interpolate from")
    k = np.asarray(k, dtype=float)
    ts = [n[0] for n in nodes]
    if t <= ts[0]:
        return np.asarray(svi_w(nodes[0][1], k)) * (t / ts[0])
    if t >= ts[-1]:
        return np.asarray(svi_w(nodes[-1][1], k)) * (t / ts[-1])
    j = int(np.searchsorted(ts, t))  # ts[j-1] < t <= ts[j]
    (t1, p1), (t2, p2) = nodes[j - 1], nodes[j]
    lam = (t - t1) / (t2 - t1)
    return (1 - lam) * np.asarray(svi_w(p1, k)) + lam * np.asarray(svi_w(p2, k))
