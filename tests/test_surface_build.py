import numpy as np
import pandas as pd
import pytest

from mm.pricing.black76 import price_eth
from mm.surface.build import build_refits
from mm.surface.essvi import ESSVI
from mm.surface.svi import svi_vol

R0 = pd.Timestamp("2025-03-03 12:00", tz="UTC")
EXP = pd.Timestamp("2025-03-28 08:00", tz="UTC")
TRUE = ESSVI(theta=0.01, rho=-0.3, psi=0.045).to_svi()


def _priced(n_min=55, strikes=(1700, 1850, 2000, 2150, 2300, 2450), f=2000.0):
    rows = []
    for j in range(n_min):
        ts = R0 - pd.Timedelta(minutes=n_min - j)
        k_s = strikes[j % len(strikes)]
        t = (EXP - ts).total_seconds() / (365 * 86400)
        k = np.log(k_s / f)
        iv = float(svi_vol(TRUE.scaled(t / 0.068), k, t))
        rows.append(
            {
                "ts": ts,
                "expiry": EXP,
                "strike_usd": float(k_s),
                "option_type": "C",
                "forward_usd": f,
                "t_years": t,
                "mark_iv": iv,
                "mark_iv_status": 0,
                "mark_price_eth": price_eth(f, k_s, t, iv, True),
            }
        )
    return pd.DataFrame(rows)


def test_surface_at_R_ignores_marks_at_or_after_R(cfg):
    base = _priced()
    spike = base.iloc[[-1]].assign(ts=R0, mark_iv=3.0)  # a wild mark exactly at R0
    later = base.iloc[[-1]].assign(ts=R0 + pd.Timedelta("1s"), mark_iv=3.0)
    a = build_refits(base, cfg, refit_times=[R0])
    b = build_refits(pd.concat([base, spike, later], ignore_index=True), cfg, refit_times=[R0])
    cols = ["kind", "theta", "rho_e", "psi"]
    pd.testing.assert_frame_equal(a.fits[cols], b.fits[cols])


def test_build_produces_full_fit_and_stats(cfg):
    out = build_refits(_priced(), cfg, refit_times=[R0])
    row = out.fits.iloc[0]
    assert row["kind"] == "full" and row["n_strikes"] == 6
    assert row["rmse_volpts"] < 0.05
    assert not out.refits["any_butterfly"].iloc[0]
    assert not out.refits["any_calendar"].iloc[0]


def test_build_is_independent_of_worker_count(cfg):
    times = [R0 - pd.Timedelta(minutes=5), R0]
    one = build_refits(
        _priced(),
        cfg.model_copy(update={"surface": cfg.surface.model_copy(update={"n_workers": 1})}),
        refit_times=times,
    )
    many = build_refits(
        _priced(),
        cfg.model_copy(update={"surface": cfg.surface.model_copy(update={"n_workers": 4})}),
        refit_times=times,
    )
    pd.testing.assert_frame_equal(one.fits, many.fits)


def test_recency_weights_decay_with_age():
    from mm.surface.build import recency_weights

    r = 10_000_000
    w = recency_weights(np.array([r - 600_000, r - 1, r - 3_000_000]), r, tau_s=600)
    assert w[0] == pytest.approx(np.exp(-1))
    assert w[1] == pytest.approx(1.0, abs=1e-5)
    assert w[2] == pytest.approx(np.exp(-5))


def test_recent_marks_dominate_the_level(cfg):
    # Old marks (50 min ago) 5 vol points higher than recent ones at the same strikes:
    # the fitted ATM vol should sit near the recent level.
    base = _priced(n_min=55)
    old = base[base["ts"] < R0 - pd.Timedelta("40min")].assign(
        mark_iv=lambda d: d["mark_iv"] + 0.05
    )
    recent = base[base["ts"] >= R0 - pd.Timedelta("40min")]
    out = build_refits(pd.concat([old, recent]), cfg, refit_times=[R0])
    t = out.fits["t_ref"].iloc[0]
    atm = float(np.sqrt(out.fits["theta"].iloc[0] / t))
    atm_true = float(svi_vol(TRUE.scaled(t / 0.068), 0.0, t))
    assert abs(atm - atm_true) < 0.01
