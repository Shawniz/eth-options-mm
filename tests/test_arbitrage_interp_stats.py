import numpy as np
import pytest

from mm.pricing.black76 import delta
from mm.surface.arbitrage import calendar_violations
from mm.surface.interp import total_variance_interp
from mm.surface.stats import atm_vol, bf25, rr25, strike_for_delta
from mm.surface.svi import SVI, svi_vol, svi_w

S1 = SVI(0.002, 0.02, -0.3, 0.0, 0.08)  # T1 = 10d
S2 = SVI(0.006, 0.03, -0.3, 0.0, 0.10)  # T2 = 40d
T1, T2 = 10 / 365, 40 / 365
K = np.linspace(-1, 1, 201)


def test_calendar_clean_and_violation():
    assert calendar_violations([(T1, S1), (T2, S2)], K, tol=1e-9) == []
    bad = SVI(0.001, 0.01, -0.3, 0.0, 0.08)  # lower total variance at the longer expiry
    assert calendar_violations([(T1, S1), (T2, bad)], K, tol=1e-9) != []


def test_interp_is_linear_in_total_variance_and_exact_at_nodes():
    nodes = [(T1, S1), (T2, S2)]
    assert total_variance_interp(nodes, K, T1) == pytest.approx(svi_w(S1, K))
    mid = total_variance_interp(nodes, K, 25 / 365)
    assert mid == pytest.approx(0.5 * (svi_w(S1, K) + svi_w(S2, K)))


def test_interp_outside_nodes_holds_vol_flat():
    nodes = [(T1, S1), (T2, S2)]
    short = total_variance_interp(nodes, K, 5 / 365)
    assert np.sqrt(short / (5 / 365)) == pytest.approx(svi_vol(S1, K, T1))
    long = total_variance_interp(nodes, K, 80 / 365)
    assert np.sqrt(long / (80 / 365)) == pytest.approx(svi_vol(S2, K, T2))


def test_25_delta_strike_uses_the_smiles_own_vol():
    t = 30 / 365
    for is_call, target in [(True, 0.25), (False, -0.25)]:
        k = strike_for_delta(S2, t, target=target, is_call=is_call)
        vol = svi_vol(S2, k, t)
        assert delta(1.0, np.exp(k), t, vol, is_call) == pytest.approx(target, abs=1e-9)


def test_rr_negative_for_put_skew_and_bf_positive_for_smile():
    t = 30 / 365
    assert rr25(S2, t) < 0  # rho < 0: puts richer than calls
    assert bf25(S2, t) > 0  # convex smile
    assert atm_vol(S2, t) == pytest.approx(np.sqrt(svi_w(S2, 0.0) / t))


def test_stats_accept_an_interpolated_smile():
    t = 25 / 365
    nodes = [(T1, S1), (T2, S2)]

    def vol(k):
        return np.sqrt(total_variance_interp(nodes, np.atleast_1d(k), t) / t)

    k = strike_for_delta(vol, t, target=0.25, is_call=True)
    assert delta(1.0, np.exp(k), t, float(vol(k)[0]), True) == pytest.approx(0.25, abs=1e-9)
