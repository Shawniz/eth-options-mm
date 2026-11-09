import numpy as np
import pandas as pd
import pytest

from mm.surface.essvi import ESSVI, psi_min
from mm.surface.fit import fit_expiry, select_window, to_moneyness
from mm.surface.svi import svi_vol, svi_w

R = pd.Timestamp("2025-03-03 12:00", tz="UTC")
TRUE = ESSVI(theta=0.0075, rho=-0.3, psi=0.035)
T = 20 / 365


def _marks(e, ks, t=T, noise=0.0, vega=None, seed=0):
    rng = np.random.default_rng(seed)
    vol = svi_vol(e.to_svi(), ks, t) + noise * rng.standard_normal(len(ks))
    return pd.DataFrame(
        {
            "ts": R - pd.Timedelta("10min"),
            "k": ks,
            "mark_iv": vol,
            "t_years": t,
            "vega": vega if vega is not None else 1.0,
            "strike_usd": np.round(2000 * np.exp(ks), 6),
        }
    )


def test_recovers_true_smile_without_noise(cfg):
    ks = np.linspace(-0.3, 0.3, 9)
    fit = fit_expiry(_marks(TRUE, ks), prev=None, cfg=cfg.surface, t_ref=T)
    assert fit.kind == "full"
    assert svi_vol(fit.params, ks, T) == pytest.approx(svi_vol(TRUE.to_svi(), ks, T), abs=1e-6)
    assert fit.rmse_volpts < 1e-3


def test_four_strikes_either_side_of_forward_give_a_full_fit(cfg):
    ks = np.array([-0.1, -0.03, 0.04, 0.1])
    fit = fit_expiry(_marks(TRUE, ks), prev=None, cfg=cfg.surface, t_ref=T)
    assert fit.kind == "full"
    assert fit.essvi.theta == pytest.approx(TRUE.theta, rel=1e-4)


def test_three_strikes_are_not_enough_for_a_full_fit(cfg):
    # 3 points, 3 parameters: an exact fit with an arbitrary ATM level (D026)
    fit = fit_expiry(_marks(TRUE, np.array([-0.1, 0.0, 0.1])), prev=None, cfg=cfg.surface, t_ref=T)
    assert fit.kind == "none"


def test_strikes_all_on_one_side_do_not_give_a_full_fit(cfg):
    # many OTM calls but nothing below the forward: ATM would be extrapolated (D026)
    prev = fit_expiry(_marks(TRUE, np.linspace(-0.3, 0.3, 9)), prev=None, cfg=cfg.surface, t_ref=T)
    m = _marks(ESSVI(0.012, -0.3, 0.035), np.linspace(0.05, 0.4, 8))
    fit = fit_expiry(m, prev=prev, cfg=cfg.surface, t_ref=T, prev_age_s=600)
    assert fit.kind == "carry"
    assert fit.essvi.theta == pytest.approx(prev.essvi.theta)


def test_vega_weighting_downweights_wing_outlier(cfg):
    ks = np.linspace(-0.3, 0.3, 9)
    m = _marks(TRUE, ks, vega=np.where(np.abs(ks) > 0.25, 0.01, 1.0))
    m.loc[m["k"].idxmax(), "mark_iv"] += 0.10  # 10-vol outlier in the low-vega wing
    fit = fit_expiry(m, prev=None, cfg=cfg.surface, t_ref=T)
    assert abs(svi_vol(fit.params, 0.0, T) - svi_vol(TRUE.to_svi(), 0.0, T)) < 0.002


def test_warm_start_gives_same_answer(cfg):
    m = _marks(TRUE, np.linspace(-0.3, 0.3, 9), noise=0.002)
    cold = fit_expiry(m, prev=None, cfg=cfg.surface, t_ref=T)
    warm = fit_expiry(m, prev=cold, cfg=cfg.surface, t_ref=T)
    assert svi_vol(warm.params, 0.0, T) == pytest.approx(svi_vol(cold.params, 0.0, T), abs=1e-5)


def test_fit_respects_calendar_bound_from_shorter_slice(cfg):
    # The shorter expiry has MORE total variance than these marks imply: the fit must
    # still sit on or above it (calendar), at the cost of fit error.
    shorter = ESSVI(theta=0.009, rho=-0.3, psi=0.04)
    fit = fit_expiry(
        _marks(TRUE, np.linspace(-0.3, 0.3, 9)),
        prev=None,
        cfg=cfg.surface,
        t_ref=T,
        shorter=shorter,
    )
    e = fit.essvi
    assert e.theta >= shorter.theta - 1e-12
    assert e.psi >= psi_min(e.rho, shorter) - 1e-12
    k = np.linspace(-1, 1, 201)
    assert (svi_w(fit.params, k) - svi_w(shorter.to_svi(), k)).min() >= -1e-10


def test_few_strikes_carry_or_none(cfg):
    prev = fit_expiry(_marks(TRUE, np.linspace(-0.3, 0.3, 9)), prev=None, cfg=cfg.surface, t_ref=T)
    one = _marks(TRUE, np.array([0.0]))
    carried = fit_expiry(one, prev=prev, cfg=cfg.surface, t_ref=T, prev_age_s=600)
    assert carried.kind == "carry" and carried.essvi == prev.essvi
    stale = cfg.surface.max_carry_s + 1
    assert fit_expiry(one, prev=prev, cfg=cfg.surface, t_ref=T, prev_age_s=stale).kind == "none"
    assert fit_expiry(one, prev=None, cfg=cfg.surface, t_ref=T).kind == "none"


def test_carry_is_projected_onto_calendar_bound(cfg):
    prev = fit_expiry(_marks(TRUE, np.linspace(-0.3, 0.3, 9)), prev=None, cfg=cfg.surface, t_ref=T)
    shorter = ESSVI(theta=prev.essvi.theta * 1.2, rho=-0.3, psi=prev.essvi.psi * 1.1)
    carried = fit_expiry(
        _marks(TRUE, np.array([])),
        prev=prev,
        cfg=cfg.surface,
        t_ref=T,
        prev_age_s=60,
        shorter=shorter,
    )
    assert carried.kind == "carry"
    k = np.linspace(-1, 1, 201)
    assert (svi_w(carried.params, k) - svi_w(shorter.to_svi(), k)).min() >= -1e-10


def test_carry_rescales_to_new_expiry_time(cfg):
    prev = fit_expiry(_marks(TRUE, np.linspace(-0.3, 0.3, 9)), prev=None, cfg=cfg.surface, t_ref=T)
    t_new = T - 300 / (365 * 86400)
    carried = fit_expiry(
        _marks(TRUE, np.array([])), prev=prev, cfg=cfg.surface, t_ref=t_new, prev_age_s=300
    )
    assert svi_vol(carried.params, 0.0, t_new) == pytest.approx(svi_vol(prev.params, 0.0, T))


def test_window_is_strictly_before_refit_time(cfg):
    w = cfg.surface.trailing_window_s
    obs = pd.DataFrame(
        {
            "ts": [
                R - pd.Timedelta(seconds=w + 1),
                R - pd.Timedelta(seconds=w),
                R - pd.Timedelta("1s"),
                R,
                R + pd.Timedelta("1s"),
            ]
        }
    )
    got = select_window(obs, R, cfg.surface)
    assert got["ts"].tolist() == [R - pd.Timedelta(seconds=w), R - pd.Timedelta("1s")]


def test_pooling_by_moneyness_uses_each_marks_own_forward():
    df = pd.DataFrame({"strike_usd": [2000.0, 2000.0], "forward_usd": [2000.0, 2040.0]})
    assert to_moneyness(df).tolist() == pytest.approx([0.0, np.log(2000 / 2040)])


def test_bad_warm_start_is_retried_from_cold_starts(cfg):
    # Previous fit stuck in the wrong region (positive skew, far too high): a warm start
    # alone can stay there; the retry must find the true smile.
    from mm.surface.fit import Fit

    bad_prev = Fit("full", ESSVI(theta=0.04, rho=0.9, psi=0.05), T, 9, 9, 50.0)
    m = _marks(TRUE, np.linspace(-0.3, 0.3, 9))
    fit = fit_expiry(m, prev=bad_prev, cfg=cfg.surface, t_ref=T)
    assert fit.kind == "full"
    assert fit.rmse_volpts < cfg.surface.retry_rmse_volpts
    assert svi_vol(fit.params, 0.0, T) == pytest.approx(svi_vol(TRUE.to_svi(), 0.0, T), abs=1e-4)


def test_absurd_fit_is_rejected_not_used(cfg):
    # Marks that no smile can fit (alternating 30% / 150% vols): reject, then carry.
    from mm.surface.fit import Fit

    ks = np.linspace(-0.3, 0.3, 9)
    m = _marks(TRUE, ks).assign(mark_iv=np.where(np.arange(9) % 2 == 0, 0.3, 1.5))
    prev = Fit("full", TRUE, T, 9, 9, 0.1)
    fit = fit_expiry(m, prev=prev, cfg=cfg.surface, t_ref=T, prev_age_s=300)
    assert fit.kind == "carry" and fit.note == "rejected"
    assert fit_expiry(m, prev=None, cfg=cfg.surface, t_ref=T).kind == "none"
