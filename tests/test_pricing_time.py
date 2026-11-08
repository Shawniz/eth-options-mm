from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from mm.pricing.time import MS_PER_YEAR, year_frac


def test_ms_per_year_is_act365():
    assert MS_PER_YEAR == 365 * 86_400_000


def test_year_frac_ints_ms():
    t = 1735689600000  # 2025-01-01 00:00 UTC
    e = t + 365 * 86_400_000
    assert year_frac(t, e) == pytest.approx(1.0)
    assert year_frac(t, t + 3_600_000) == pytest.approx(1 / (365 * 24))


def test_year_frac_millisecond_precision():
    t = np.array([0, 1], dtype="int64")
    d = year_frac(t, np.array([1000, 1000], dtype="int64"))
    assert d[0] - d[1] == pytest.approx(1 / MS_PER_YEAR)


def test_year_frac_pandas_tz_aware():
    ts = pd.Series(pd.to_datetime(["2025-02-27 08:00", "2025-02-28 07:00"], utc=True))
    exp = pd.Series(pd.to_datetime(["2025-02-28 08:00", "2025-02-28 08:00"], utc=True))
    out = year_frac(ts, exp)
    assert out.tolist() == pytest.approx([1 / 365, 1 / (365 * 24)])


def test_year_frac_datetime_scalars():
    t = datetime(2025, 1, 1, tzinfo=UTC)
    e = datetime(2025, 1, 2, tzinfo=UTC)
    assert year_frac(t, e) == pytest.approx(1 / 365)


def test_year_frac_negative_after_expiry():
    # No silent clipping: callers decide what to do with expired options.
    assert year_frac(10, 0) < 0


def test_year_frac_rejects_naive_datetimes():
    with pytest.raises(ValueError, match="timezone"):
        year_frac(datetime(2025, 1, 1), datetime(2025, 1, 2))
