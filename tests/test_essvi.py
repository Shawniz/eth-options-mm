"""eSSVI slices. Property tests check the no-arbitrage CONDITIONS themselves against the
numerical checks, so a wrong condition fails here instead of letting arbitrage through.
(One already did: see test_wing_conditions_alone_are_not_sufficient.)"""

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mm.surface.essvi import ESSVI, psi_max, psi_min
from mm.surface.svi import butterfly_g, svi_w

K = np.linspace(-3, 3, 601)


def _w(e: ESSVI, k):
    phi = e.psi / e.theta
    return e.theta / 2 * (1 + e.rho * phi * k + np.sqrt((phi * k + e.rho) ** 2 + 1 - e.rho**2))


def test_raw_svi_mapping_matches_essvi_formula():
    e = ESSVI(theta=0.02, rho=-0.35, psi=0.08)
    assert svi_w(e.to_svi(), K) == pytest.approx(_w(e, K), rel=1e-12)


def test_atm_total_variance_is_theta():
    e = ESSVI(theta=0.02, rho=-0.35, psi=0.08)
    assert svi_w(e.to_svi(), 0.0) == pytest.approx(0.02)


def test_scaling_scales_theta_and_psi():
    e = ESSVI(0.02, -0.35, 0.08)
    assert svi_w(e.scaled(1.5).to_svi(), K) == pytest.approx(1.5 * svi_w(e.to_svi(), K))


thetas = st.floats(1e-4, 0.5)
rhos = st.floats(-0.95, 0.95)
fracs = st.floats(0.01, 1.0)


@settings(max_examples=300)
@given(thetas, rhos, fracs)
def test_butterfly_condition_gives_non_negative_density(theta, rho, u):
    e = ESSVI(theta, rho, u * psi_max(theta, rho))
    k = K * np.sqrt(theta) * 4  # wide grid in the slice's own scale
    assert butterfly_g(e.to_svi(), k).min() >= -1e-9


@settings(max_examples=300)
@given(thetas, rhos, fracs, st.floats(1.0, 3.0), rhos, fracs)
def test_psi_min_orders_both_wing_slopes(th1, r1, u1, grow, r2, u2):
    # psi >= psi_min is the NECESSARY calendar condition: both asymptotic wing slopes of
    # the longer slice are at least those of the shorter one.
    e1 = ESSVI(th1, r1, u1 * psi_max(th1, r1))
    th2 = th1 * grow
    lo, hi = psi_min(r2, e1), psi_max(th2, r2)
    if lo > hi:
        return
    e2 = ESSVI(th2, r2, lo + u2 * (hi - lo))
    assert e2.psi * (1 + e2.rho) >= e1.psi * (1 + e1.rho) - 1e-12
    assert e2.psi * (1 - e2.rho) >= e1.psi * (1 - e1.rho) - 1e-12


def test_wing_conditions_alone_are_not_sufficient():
    # Found by hypothesis (2026-11-09): equal ATM variance, the shorter slice skewed, the
    # longer flat. Wing slopes are ordered, yet w_short > w_long just right of ATM. This
    # is why the fitter also enforces w_long(k) >= w_short(k) on a grid (decision D023).
    e1 = ESSVI(0.5, 0.5, 0.5 * psi_max(0.5, 0.5))
    e2 = ESSVI(0.5, 0.0, psi_max(0.5, 0.0))
    assert e2.psi >= psi_min(0.0, e1)
    k = np.linspace(0.01, 0.5, 50)
    assert (svi_w(e2.to_svi(), k) - svi_w(e1.to_svi(), k)).min() < 0


def test_psi_min_without_previous_slice_is_tiny():
    assert psi_min(0.3, None) < 1e-6
