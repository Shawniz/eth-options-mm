import numpy as np
import pandas as pd
import pytest

from mm.surface.fit import fit_expiry, select_window, to_moneyness
from mm.surface.svi import SVI, svi_vol

R = pd.Timestamp("2025-03-03 12:00", tz="UTC")
TRUE = SVI(a=0.004, b=0.03, rho=-0.3, m=0.01, s=0.08)
T = 20 / 365


def _marks(params, ks, t=T, noise=0.0, vega=None, seed=0):
    rng = np.random.default_rng(seed)
    vol = svi_vol(params, ks, t) + noise * rng.standard_normal(len(ks))
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
    assert svi_vol(fit.params, ks, T) == pytest.approx(svi_vol(TRUE, ks, T), abs=1e-6)
    assert fit.rmse_volpts < 1e-3


def test_vega_weighting_downweights_wing_outlier(cfg):
    ks = np.linspace(-0.3, 0.3, 9)
    m = _marks(TRUE, ks, vega=np.where(np.abs(ks) > 0.25, 0.01, 1.0))
    m.loc[m["k"].idxmax(), "mark_iv"] += 0.10  # 10-vol outlier in the low-vega wing
    fit = fit_expiry(m, prev=None, cfg=cfg.surface, t_ref=T)
    assert abs(svi_vol(fit.params, 0.0, T) - svi_vol(TRUE, 0.0, T)) < 0.002


def test_warm_start_gives_same_answer(cfg):
    m = _marks(TRUE, np.linspace(-0.3, 0.3, 9), noise=0.002)
    cold = fit_expiry(m, prev=None, cfg=cfg.surface, t_ref=T)
    warm = fit_expiry(m, prev=cold, cfg=cfg.surface, t_ref=T)
    assert svi_vol(warm.params, 0.0, T) == pytest.approx(svi_vol(cold.params, 0.0, T), abs=1e-5)


def test_three_strikes_refit_level_only(cfg):
    prev = fit_expiry(_marks(TRUE, np.linspace(-0.3, 0.3, 9)), prev=None, cfg=cfg.surface, t_ref=T)
    bumped = SVI(
        prev.params.a + 0.001, prev.params.b, prev.params.rho, prev.params.m, prev.params.s
    )
    fit = fit_expiry(
        _marks(bumped, np.array([-0.05, 0.0, 0.05])), prev=prev, cfg=cfg.surface, t_ref=T
    )
    assert fit.kind == "level"
    p, q = fit.params, prev.params
    assert (p.b, p.rho, p.m, p.s) == (q.b, q.rho, q.m, q.s)
    assert p.a == pytest.approx(bumped.a, abs=1e-6)


def test_level_fit_without_previous_gives_none(cfg):
    fit = fit_expiry(
        _marks(TRUE, np.array([-0.05, 0.0, 0.05])), prev=None, cfg=cfg.surface, t_ref=T
    )
    assert fit.kind == "none"


def test_too_few_strikes_carries_or_gives_none(cfg):
    prev = fit_expiry(_marks(TRUE, np.linspace(-0.3, 0.3, 9)), prev=None, cfg=cfg.surface, t_ref=T)
    one = _marks(TRUE, np.array([0.0]))
    carried = fit_expiry(one, prev=prev, cfg=cfg.surface, t_ref=T, prev_age_s=600)
    assert carried.kind == "carry" and carried.params == prev.params
    stale = cfg.surface.max_carry_s + 1
    assert fit_expiry(one, prev=prev, cfg=cfg.surface, t_ref=T, prev_age_s=stale).kind == "none"


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
    # Same strike, spot moved 2% inside the window: two different k.
    df = pd.DataFrame({"strike_usd": [2000.0, 2000.0], "forward_usd": [2000.0, 2040.0]})
    assert to_moneyness(df).tolist() == pytest.approx([0.0, np.log(2000 / 2040)])
