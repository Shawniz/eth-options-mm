"""Static-arbitrage checks on a k grid (flagged, not enforced; decision D022).

butterfly: Gatheral g(k) >= 0 within each smile (non-negative implied density)
calendar:  total variance non-decreasing in T at every k (consecutive expiries)
"""

from __future__ import annotations

import numpy as np

from mm.surface.svi import SVI, butterfly_g, svi_w


def butterfly_min(p: SVI, k: np.ndarray) -> float:
    return float(np.nanmin(butterfly_g(p, k)))


def calendar_violations(nodes: list[tuple[float, SVI]], k: np.ndarray, tol: float):
    """[(t1, t2, worst w2 - w1)] for consecutive expiries where w(k, t2) < w(k, t1) - tol."""
    nodes = sorted(nodes, key=lambda n: n[0])
    out = []
    for (t1, p1), (t2, p2) in zip(nodes, nodes[1:], strict=False):
        diff = np.asarray(svi_w(p2, k)) - np.asarray(svi_w(p1, k))
        if diff.min() < -tol:
            out.append((t1, t2, float(diff.min())))
    return out
