"""Book Greek aggregation (USD). Phase 5: vega for inventory skew; Phase 6 extends."""

from __future__ import annotations

import numpy as np

from mm.pricing.black76 import vega


def book_vega_usd(n, f, k, t, sigma, s) -> float:
    """sum n_i x Black vega_i x S/F: USD per 1 vol point of account value
    (an option is worth n (C/F) S in USD, so dW/dsigma = n (S/F) dC/dsigma).
    Positions with no valid inputs contribute 0."""
    n, f, s = (np.asarray(v, dtype=float) for v in (n, f, s))
    v = np.asarray(vega(f, k, t, sigma), dtype=float) * s / f
    return float(np.nansum(n * v))
