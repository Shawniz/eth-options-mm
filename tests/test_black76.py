from datetime import UTC, datetime

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from mm.pricing.black76 import delta, gamma, price_eth, price_usd, theta, vega

F, K, T, SIG = 3300.0, 3500.0, 30 / 365, 0.65


def test_put_call_parity_usd():
    # Undiscounted Black-76: C - P = F - K
    c = price_usd(F, K, T, SIG, is_call=True)
    p = price_usd(F, K, T, SIG, is_call=False)
    assert c - p == pytest.approx(F - K, abs=1e-9)


def test_put_call_parity_eth():
    # Divide by F: c - p = 1 - K/F  (what Deribit's ETH premiums obey)
    c = price_eth(F, K, T, SIG, is_call=True)
    p = price_eth(F, K, T, SIG, is_call=False)
    assert c - p == pytest.approx(1 - K / F, abs=1e-12)


def test_eth_price_is_usd_price_over_forward():
    assert price_eth(F, K, T, SIG, True) == pytest.approx(price_usd(F, K, T, SIG, True) / F)


def test_known_value_deribit_ticker_2026_11_05():
    # Hand-checked against a live ETH-25DEC26-3200-C ticker (ts 1793926511466)
    expiry_ms = datetime(2026, 12, 25, 8, tzinfo=UTC).timestamp() * 1000
    t = (expiry_ms - 1793926511466) / (365 * 86_400_000)
    assert price_eth(2254.93, 3200.0, t, 0.5823, True) == pytest.approx(0.005421, abs=2e-6)
    assert delta(2254.93, 3200.0, t, 0.5823, True) == pytest.approx(0.06317, abs=1e-5)
    assert gamma(2254.93, 3200.0, t, 0.5823) == pytest.approx(0.00026, abs=5e-6)
    assert vega(2254.93, 3200.0, t, 0.5823) == pytest.approx(1.0276, abs=1e-4)
    assert theta(2254.93, 3200.0, t, 0.5823) == pytest.approx(-0.60692, abs=1e-4)


@pytest.mark.parametrize("is_call", [True, False])
def test_greeks_match_finite_differences(is_call):
    h = 0.01
    up = price_usd(F + h, K, T, SIG, is_call)
    dn = price_usd(F - h, K, T, SIG, is_call)
    mid = price_usd(F, K, T, SIG, is_call)
    assert delta(F, K, T, SIG, is_call) == pytest.approx((up - dn) / (2 * h), rel=1e-6)
    assert gamma(F, K, T, SIG) == pytest.approx((up - 2 * mid + dn) / h**2, rel=1e-4)
    # vega per 1 vol point (0.01), in USD
    hv = 0.0005  # small bump: a 1-point bump has visible O(h^2) curvature error
    v_fd = (price_usd(F, K, T, SIG + hv, is_call) - price_usd(F, K, T, SIG - hv, is_call)) * (
        0.01 / (2 * hv)
    )
    assert vega(F, K, T, SIG) == pytest.approx(v_fd, rel=1e-6)
    # theta per calendar day, in USD (value change as one day passes)
    d = 1 / 365
    th_fd = price_usd(F, K, T - d, SIG, is_call) - price_usd(F, K, T, SIG, is_call)
    assert theta(F, K, T, SIG) == pytest.approx(th_fd, rel=1e-2)


def test_put_delta_is_call_delta_minus_one():
    assert delta(F, K, T, SIG, False) == pytest.approx(delta(F, K, T, SIG, True) - 1)


def test_vectorised_and_scalar():
    k = np.array([2500.0, 3300.0, 4500.0])
    out = price_eth(F, k, T, SIG, np.array([False, True, True]))
    assert out.shape == (3,)
    assert isinstance(price_eth(F, K, T, SIG, True), float)


def test_invalid_inputs_give_nan_not_garbage():
    assert np.isnan(price_eth(F, K, 0.0, SIG, True))
    assert np.isnan(price_eth(F, K, T, 0.0, True))
    assert np.isnan(price_eth(F, K, -1.0, SIG, True))


# ---- property-based tests (hypothesis): must hold for ANY valid input ----

fwd = st.floats(500, 20_000)
moneyness = st.floats(0.3, 3.0)  # K / F
tenor = st.floats(1 / (365 * 24), 2.0)  # 1 hour to 2 years
vol = st.floats(0.05, 3.0)


@given(fwd, moneyness, tenor, vol, st.booleans())
def test_price_within_no_arbitrage_bounds(f, m, t, s, is_call):
    k = f * m
    p = price_eth(f, k, t, s, is_call)
    intrinsic = max(1 - k / f, 0) if is_call else max(k / f - 1, 0)
    upper = 1.0 if is_call else k / f
    assert intrinsic - 1e-10 <= p <= upper + 1e-10


@given(fwd, tenor, vol)
def test_call_delta_decreases_with_strike(f, t, s):
    ks = f * np.linspace(0.5, 2.0, 25)
    d = delta(f, ks, t, s, True)
    assert np.all(np.diff(d) <= 1e-12)


@given(fwd, moneyness, tenor, vol)
def test_price_increases_with_vol(f, m, t, s):
    k = f * m
    assert price_eth(f, k, t, s * 1.01, True) >= price_eth(f, k, t, s, True) - 1e-12
