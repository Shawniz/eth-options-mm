import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from mm.pricing.black76 import price_eth
from mm.pricing.iv import Status, implied_vol, vol_for_price_change


def test_round_trip_grid():
    f = 3300.0
    k = f * np.array([0.6, 0.8, 0.95, 1.0, 1.05, 1.25, 1.6])
    for t in [1 / 365, 7 / 365, 30 / 365, 90 / 365]:
        for s in [0.3, 0.65, 1.2]:
            for is_call in (True, False):
                p = price_eth(f, k, t, s, is_call)
                intrinsic = np.maximum(1 - k / f, 0) if is_call else np.maximum(k / f - 1, 0)
                tv = p - intrinsic
                iv, status = implied_vol(p, f, k, t, is_call)
                # wherever there is real time value the solver must succeed and be exact
                solid = tv > 1e-6
                assert (status[solid] == Status.OK).all()
                assert iv[solid] == pytest.approx(np.full(solid.sum(), s), abs=1e-8)
                # where time value is tiny, success means the price reprices exactly
                ok = status == Status.OK
                rep = price_eth(f, k[ok], t, iv[ok], is_call)
                assert rep == pytest.approx(p[ok], abs=1e-12)  # tick is 1e-4


def test_out_of_bounds_prices_flagged_not_guessed():
    f, k, t = 3300.0, 3000.0, 7 / 365
    below = (1 - k / f) * 0.99  # call below intrinsic
    iv, status = implied_vol(np.array([below, 1.5]), f, k, t, True)
    assert np.isnan(iv).all()
    assert list(status) == [Status.BELOW_INTRINSIC, Status.ABOVE_MAX]


def test_no_time_value_flagged():
    f, k, t = 3300.0, 3000.0, 7 / 365
    iv, status = implied_vol(np.array([1 - k / f]), f, k, t, True)  # exactly intrinsic
    assert np.isnan(iv[0]) and status[0] == Status.NO_TIME_VALUE


def test_put_and_call_give_same_vol():
    f, k, t, s = 3300.0, 3100.0, 10 / 365, 0.8
    ivc, _ = implied_vol(np.array([price_eth(f, k, t, s, True)]), f, k, t, True)
    ivp, _ = implied_vol(np.array([price_eth(f, k, t, s, False)]), f, k, t, False)
    assert ivc[0] == pytest.approx(ivp[0], abs=1e-9)


def test_vol_change_uses_exact_repricing_not_vega():
    # A big price move: the vega approximation is visibly wrong, exact repricing is not.
    f, k, t, s = 3300.0, 4500.0, 14 / 365, 0.7
    p0 = price_eth(f, k, t, s, True)
    dp = 3 * p0
    ds = vol_for_price_change(p0, dp, f, k, t, s, True)
    assert price_eth(f, k, t, s + ds, True) == pytest.approx(p0 + dp, rel=1e-10)


@given(st.floats(0.6, 1.6), st.floats(2 / 365, 1.0), st.floats(0.1, 2.0), st.booleans())
def test_round_trip_property(m, t, s, is_call):
    f = 3000.0
    p = price_eth(f, f * m, t, s, is_call)
    iv, status = implied_vol(np.array([p]), f, f * m, t, is_call)
    if status[0] == Status.OK:
        assert price_eth(f, f * m, t, iv[0], is_call) == pytest.approx(p, abs=1e-12)
