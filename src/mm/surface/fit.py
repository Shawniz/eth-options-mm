"""Fit one expiry's SVI smile to mark IVs, with the minimum-data ladder (decision D021).

    >= min_strikes_full distinct strikes        full   all 5 parameters, warm-started
    >= min_strikes_level (and a previous fit)  level  previous shape, refit level `a` only
    fewer, previous fit younger than max_carry  carry  previous fit, rescaled in time
    otherwise                                   none   no smile (neighbours interpolate)

Objective: vega-weighted squared vol error, sum_i vega_i (sigma_svi(k_i) - sigma_i)^2, all
marks evaluated at the refit's time to expiry t_ref (sticky vol over the window).
Positivity of w is built in by fitting e = a + b s sqrt(1 - rho^2) >= 0 instead of a.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import least_squares

from mm.config import SurfaceConfig
from mm.surface.svi import SVI, svi_vol, svi_w

# Parameter bounds for (e, b, rho, m, s). Wide enough never to bind on real smiles.
_LO = np.array([0.0, 0.0, -0.999, -1.0, 1e-4])
_HI = np.array([np.inf, 10.0, 0.999, 1.0, 3.0])


@dataclass(frozen=True)
class Fit:
    kind: str  # full | level | carry | none
    params: SVI | None
    t_ref: float
    n_obs: int
    n_strikes: int
    rmse_volpts: float  # vega-weighted RMSE vs the window's marks (nan if none)


def to_moneyness(df: pd.DataFrame) -> pd.Series:
    """k = ln(K / F) with each mark's OWN forward (pooling in moneyness)."""
    return np.log(df["strike_usd"] / df["forward_usd"])


def select_window(obs: pd.DataFrame, refit_ts: pd.Timestamp, cfg: SurfaceConfig) -> pd.DataFrame:
    """Marks with refit_ts - window <= ts < refit_ts (strictly before the refit)."""
    lo = refit_ts - pd.Timedelta(seconds=cfg.trailing_window_s)
    return obs[(obs["ts"] >= lo) & (obs["ts"] < refit_ts)]


def _to_svi(x) -> SVI:
    e, b, rho, m, s = x
    return SVI(e - b * s * np.sqrt(1 - rho**2), b, rho, m, s)


def _from_svi(p: SVI) -> np.ndarray:
    x = np.array([max(p.min_variance, 0.0), p.b, p.rho, p.m, p.s])
    return np.clip(x, _LO + 1e-12, np.where(np.isfinite(_HI), _HI - 1e-12, np.inf))


def _rmse(p: SVI, k, vol, wt, t) -> float:
    r = svi_vol(p, k, t) - vol
    return float(np.sqrt(np.sum(wt * r * r) / np.sum(wt)) * 100.0)


def _fit_full(k, vol, wt, t, x0s) -> SVI:
    sw = np.sqrt(wt / wt.sum())

    def resid(x):
        return sw * (svi_vol(_to_svi(x), k, t) - vol)

    best = None
    for x0 in x0s:
        r = least_squares(
            resid,
            x0,
            bounds=(_LO, _HI),
            method="trf",
            x_scale="jac",
            ftol=1e-14,
            xtol=1e-14,
            gtol=1e-14,
            max_nfev=2000,
        )
        if best is None or r.cost < best.cost:
            best = r
    return _to_svi(best.x)


def _cold_starts(k, vol, t, n: int) -> list[np.ndarray]:
    w_atm = float(np.interp(0.0, np.sort(k), (vol**2 * t)[np.argsort(k)]))
    span = max(float(np.ptp(k)), 0.05)
    starts = []
    for rho in (-0.5, 0.0, 0.3):
        for s in (0.25 * span, 0.75 * span):
            b = max(w_atm, 1e-6) / max(s, 1e-3)
            starts.append(np.array([0.5 * w_atm, b, rho, 0.0, s]))
    return [np.clip(x, _LO + 1e-9, np.where(np.isfinite(_HI), _HI - 1e-9, 1e9)) for x in starts[:n]]


def _fit_level(prev: SVI, k, vol, wt, t) -> SVI:
    """Refit a only; positivity: a >= -b s sqrt(1 - rho^2)."""
    a_min = prev.a - prev.min_variance
    sw = np.sqrt(wt / wt.sum())
    shape = svi_w(SVI(0.0, prev.b, prev.rho, prev.m, prev.s), k)

    def resid(x):
        return sw * (np.sqrt(np.maximum(x[0] + shape, 0.0) / t) - vol)

    r = least_squares(
        resid,
        [max(prev.a, a_min + 1e-12)],
        bounds=([a_min], [np.inf]),
        ftol=1e-15,
        xtol=1e-15,
        gtol=1e-15,
    )
    return SVI(float(r.x[0]), prev.b, prev.rho, prev.m, prev.s)


def fit_expiry(
    marks: pd.DataFrame,
    prev: Fit | None,
    cfg: SurfaceConfig,
    t_ref: float,
    prev_age_s: float = 0.0,
) -> Fit:
    """marks: columns k, mark_iv, vega, strike_usd (already windowed and filtered)."""
    k = marks["k"].to_numpy(float)
    vol = marks["mark_iv"].to_numpy(float)
    wt = np.maximum(marks["vega"].to_numpy(float) * np.ones(len(k)), 1e-12)
    n_obs = len(k)
    n_strikes = int(marks["strike_usd"].nunique())
    prev_p = None
    if prev is not None and prev.params is not None:
        prev_p = prev.params.scaled(t_ref / prev.t_ref) if prev.t_ref != t_ref else prev.params

    if n_strikes >= cfg.min_strikes_full:
        x0s = [_from_svi(prev_p)] if prev_p is not None else []
        if prev_p is None:
            x0s = _cold_starts(k, vol, t_ref, cfg.cold_start_tries)
        p = _fit_full(k, vol, wt, t_ref, x0s)
        return Fit("full", p, t_ref, n_obs, n_strikes, _rmse(p, k, vol, wt, t_ref))
    if n_strikes >= cfg.min_strikes_level and prev_p is not None:
        p = _fit_level(prev_p, k, vol, wt, t_ref)
        return Fit("level", p, t_ref, n_obs, n_strikes, _rmse(p, k, vol, wt, t_ref))
    if prev_p is not None and prev_age_s <= cfg.max_carry_s:
        rm = _rmse(prev_p, k, vol, wt, t_ref) if n_obs else float("nan")
        return Fit("carry", prev_p, t_ref, n_obs, n_strikes, rm)
    return Fit("none", None, t_ref, n_obs, n_strikes, float("nan"))
