import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from mm.surface.svi import SVI, butterfly_g, svi_vol, svi_w

GATHERAL = SVI(a=0.04, b=0.4, rho=-0.4, m=0.0, s=0.1)  # well-behaved smile
VOGT = SVI(a=-0.0410, b=0.1331, rho=0.3060, m=0.3586, s=0.4153)  # textbook butterfly arbitrage
K = np.linspace(-1.5, 1.5, 301)


def test_formula_at_the_shift():
    p = GATHERAL
    assert svi_w(p, p.m) == pytest.approx(p.a + p.b * p.s)


def test_wings_are_asymptotically_linear():
    p = GATHERAL
    assert svi_w(p, 10.0) - svi_w(p, 9.0) == pytest.approx(p.b * (1 + p.rho), rel=1e-3)
    assert svi_w(p, -10.0) - svi_w(p, -9.0) == pytest.approx(p.b * (1 - p.rho), rel=1e-3)


def test_derivatives_match_finite_differences():
    p, h = GATHERAL, 1e-5
    w1 = (svi_w(p, K + h) - svi_w(p, K - h)) / (2 * h)
    w2 = (svi_w(p, K + h) - 2 * svi_w(p, K) + svi_w(p, K - h)) / h**2
    assert svi_w(p, K, d=1) == pytest.approx(w1, rel=1e-6, abs=1e-8)
    assert svi_w(p, K, d=2) == pytest.approx(w2, rel=1e-4, abs=1e-5)


def test_vol_is_sqrt_total_variance_over_t():
    t = 0.25
    assert svi_vol(GATHERAL, 0.1, t) == pytest.approx(np.sqrt(svi_w(GATHERAL, 0.1) / t))


def test_scaling_keeps_vols_when_time_passes():
    # sticky vol: rescale a and b by T_new / T_old, shape unchanged
    p2 = GATHERAL.scaled(0.9)
    assert svi_vol(p2, K, 0.9) == pytest.approx(svi_vol(GATHERAL, K, 1.0))


def test_butterfly_clean_smile_passes():
    assert butterfly_g(GATHERAL, K).min() >= 0


def test_butterfly_vogt_example_is_flagged():
    assert butterfly_g(VOGT, K).min() < 0


@given(
    st.floats(0.0, 0.5),
    st.floats(0.01, 1.0),
    st.floats(-0.95, 0.95),
    st.floats(-0.5, 0.5),
    st.floats(0.01, 1.0),
)
def test_variance_non_negative_under_constraint(a, b, rho, m, s):
    a = max(a, -b * s * np.sqrt(1 - rho**2))  # enforce the constraint
    assert svi_w(SVI(a, b, rho, m, s), K).min() >= -1e-12
