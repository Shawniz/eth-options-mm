"""Fit one expiry's eSSVI slice to mark IVs (decisions D021, D023).

Ladder (eSSVI has 3 parameters):
    >= min_strikes_full distinct strikes        full   (theta, rho, psi), warm-started
    fewer, previous fit younger than max_carry  carry  previous slice, rescaled in time
                                                       and kept calendar-consistent
    otherwise                                   none   no smile (neighbours interpolate)

Constraints, enforced in every fit:
    butterfly: psi <= psi_max(theta, rho)                      (by construction, any k)
    calendar vs the shorter expiry fitted at the same refit:
        theta >= theta_short, psi >= psi_min(rho, short)       (hard bounds; necessary)
        w(k) >= w_short(k) on a dense k grid                   (stiff penalty; sufficient
                                                               where it matters)
Objective: vega-weighted squared vol error at the refit's time to expiry t_ref.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import least_squares

from mm.config import SurfaceConfig
from mm.surface.essvi import ESSVI, project, psi_max, psi_min
from mm.surface.svi import SVI, svi_vol, svi_w

RHO_MAX = 0.99
THETA_FLOOR = 1e-8


@dataclass(frozen=True)
class Fit:
    kind: str  # full | carry | none
    essvi: ESSVI | None
    t_ref: float
    n_obs: int
    n_strikes: int
    rmse_volpts: float  # vega-weighted RMSE vs the window's marks (nan if none)
    note: str = ""  # "retried": warm start replaced by a cold start; "rejected": fit refused

    @property
    def params(self) -> SVI | None:
        return self.essvi.to_svi() if self.essvi is not None else None


def to_moneyness(df: pd.DataFrame) -> pd.Series:
    """k = ln(K / F) with each mark's OWN forward (pooling in moneyness)."""
    return np.log(df["strike_usd"] / df["forward_usd"])


def select_window(obs: pd.DataFrame, refit_ts: pd.Timestamp, cfg: SurfaceConfig) -> pd.DataFrame:
    """Marks with refit_ts - window <= ts < refit_ts (strictly before the refit)."""
    lo = refit_ts - pd.Timedelta(seconds=cfg.trailing_window_s)
    return obs[(obs["ts"] >= lo) & (obs["ts"] < refit_ts)]


def _calendar_grid(shorter: ESSVI | None, theta_hint: float, k, cfg: SurfaceConfig):
    if shorter is None:
        return None
    sd = np.sqrt(max(shorter.theta, theta_hint, THETA_FLOOR))
    z = cfg.calendar_grid_std
    lo = min(-z * sd, float(np.min(k)) if len(k) else 0.0)
    hi = max(z * sd, float(np.max(k)) if len(k) else 0.0)
    return np.linspace(lo, hi, cfg.calendar_grid_n)


def _decode(x, shorter):
    theta, rho, u = float(x[0]), float(x[1]), float(x[2])
    lo, hi = psi_min(rho, shorter), float(psi_max(theta, rho))
    gap = max(lo - hi, 0.0)  # > 0: bounds infeasible at this (theta, rho)
    psi = lo + u * (hi - lo) if gap == 0 else lo
    return ESSVI(theta, rho, psi), gap


def _solve(k, vol, wt, t, x0s, shorter, cfg: SurfaceConfig) -> ESSVI:
    sw = np.sqrt(wt / wt.sum())
    theta_lo = max(shorter.theta if shorter else 0.0, THETA_FLOOR)
    kg = _calendar_grid(shorter, float(np.median(vol**2 * t)) if len(vol) else 0.0, k, cfg)
    w_short = svi_w(shorter.to_svi(), kg) if shorter is not None else None
    lam = np.sqrt(cfg.calendar_penalty)
    scale = max(shorter.theta, THETA_FLOOR) if shorter is not None else 1.0

    def resid(x):
        e, gap = _decode(x, shorter)
        r = sw * (svi_vol(e.to_svi(), k, t) - vol)
        extra = [lam * gap / scale]
        if w_short is not None:
            extra.append(lam * np.minimum(svi_w(e.to_svi(), kg) - w_short, 0.0) / scale)
        return np.concatenate([r, *map(np.atleast_1d, extra)])

    lo = np.array([theta_lo, -RHO_MAX, 0.0])
    hi = np.array([np.inf, RHO_MAX, 1.0])
    best = None
    for x0 in x0s:
        x0 = np.clip(np.asarray(x0, float), lo, np.where(np.isfinite(hi), hi, 1e9))
        x0[0] = max(x0[0], theta_lo * (1 + 1e-9))
        r = least_squares(
            resid,
            x0,
            bounds=(lo, hi),
            method="trf",
            x_scale="jac",
            ftol=1e-14,
            xtol=1e-14,
            gtol=1e-14,
            max_nfev=2000,
        )
        if best is None or r.cost < best.cost:
            best = r
    return _decode(best.x, shorter)[0]


def _x_from(e: ESSVI, shorter) -> np.ndarray:
    lo, hi = psi_min(e.rho, shorter), float(psi_max(e.theta, e.rho))
    u = (e.psi - lo) / (hi - lo) if hi > lo else 0.0
    return np.array([e.theta, np.clip(e.rho, -RHO_MAX, RHO_MAX), np.clip(u, 0.0, 1.0)])


def _cold_starts(k, vol, t, n):
    order = np.argsort(k)
    theta0 = float(np.interp(0.0, k[order], (vol**2 * t)[order]))
    return [
        np.array([max(theta0, THETA_FLOOR), r, u]) for r in (-0.5, 0.0, 0.3) for u in (0.3, 0.7)
    ][:n]


def _rmse(svi: SVI, k, vol, wt, t) -> float:
    r = svi_vol(svi, k, t) - vol
    return float(np.sqrt(np.sum(wt * r * r) / np.sum(wt)) * 100.0)


def _violates(e: ESSVI, shorter: ESSVI | None, k, cfg: SurfaceConfig) -> bool:
    if shorter is None:
        return False
    kg = _calendar_grid(shorter, e.theta, k, cfg)
    return bool((svi_w(e.to_svi(), kg) - svi_w(shorter.to_svi(), kg)).min() < -cfg.calendar_tol)


def fit_expiry(
    marks: pd.DataFrame,
    prev: Fit | None,
    cfg: SurfaceConfig,
    t_ref: float,
    prev_age_s: float = 0.0,
    shorter: ESSVI | None = None,
) -> Fit:
    """marks: columns k, mark_iv, vega, strike_usd (already windowed and filtered).
    prev: this expiry's previous fit. shorter: the next-shorter expiry's slice at this
    refit (calendar constraint), or None for the shortest."""
    k = marks["k"].to_numpy(float)
    vol = marks["mark_iv"].to_numpy(float)
    wt = np.maximum(marks["vega"].to_numpy(float) * np.ones(len(k)), 1e-12)
    n_obs, n_strikes = len(k), int(marks["strike_usd"].nunique())
    prev_e = None
    if prev is not None and prev.essvi is not None:
        prev_e = prev.essvi.scaled(t_ref / prev.t_ref) if prev.t_ref != t_ref else prev.essvi

    note = ""
    if n_strikes >= cfg.min_strikes_full:
        cold = _cold_starts(k, vol, t_ref, cfg.cold_start_tries)
        if prev_e is not None:
            e = _solve(
                k, vol, wt, t_ref, [_x_from(project(prev_e, shorter), shorter)], shorter, cfg
            )
            rm = _rmse(e.to_svi(), k, vol, wt, t_ref)
            if rm > cfg.retry_rmse_volpts:  # warm start may be stuck: try the cold starts too
                e2 = _solve(k, vol, wt, t_ref, cold, shorter, cfg)
                rm2 = _rmse(e2.to_svi(), k, vol, wt, t_ref)
                if rm2 < rm:
                    e, rm, note = e2, rm2, "retried"
        else:
            e = _solve(k, vol, wt, t_ref, cold, shorter, cfg)
            rm = _rmse(e.to_svi(), k, vol, wt, t_ref)
        if rm <= cfg.reject_rmse_volpts:
            return Fit("full", e, t_ref, n_obs, n_strikes, rm, note)
        note = "rejected"  # fall through: treat as if there were no usable data

    if prev_e is not None and prev_age_s <= cfg.max_carry_s:
        e = project(prev_e, shorter)
        if _violates(e, shorter, k, cfg):
            # refit to its own previous shape under the current constraints
            sd = np.sqrt(max(prev_e.theta, THETA_FLOOR))
            kk = np.linspace(-2.5 * sd, 2.5 * sd, 21)
            vv = svi_vol(prev_e.to_svi(), kk, t_ref)
            e = _solve(kk, vv, np.ones_like(kk), t_ref, [_x_from(e, shorter)], shorter, cfg)
        rm = _rmse(e.to_svi(), k, vol, wt, t_ref) if n_obs else float("nan")
        return Fit("carry", e, t_ref, n_obs, n_strikes, rm, note)
    return Fit("none", None, t_ref, n_obs, n_strikes, float("nan"), note)
