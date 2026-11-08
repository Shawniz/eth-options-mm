"""Time to expiry in years, ACT/365, at millisecond precision.

Accepts integer milliseconds (scalars or numpy arrays), tz-aware datetimes, or pandas
tz-aware Series/Timestamps. Returns the same shape. Negative values (after expiry) are
returned as-is, never clipped: the caller decides how to treat expired options.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

MS_PER_YEAR = 365 * 86_400_000  # ACT/365


def _to_ms(x):
    if isinstance(x, pd.Series):
        if x.dt.tz is None:
            raise ValueError("timestamps must be timezone-aware (UTC)")
        return x.dt.tz_convert("UTC").astype("datetime64[ms, UTC]").astype("int64")
    if isinstance(x, datetime):  # includes pd.Timestamp
        if x.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (UTC)")
        return int(round(x.timestamp() * 1000))
    return np.asarray(x, dtype="int64") if not np.isscalar(x) else int(x)


def year_frac(t, expiry):
    """(expiry - t) in years, ACT/365."""
    return (_to_ms(expiry) - _to_ms(t)) / MS_PER_YEAR
