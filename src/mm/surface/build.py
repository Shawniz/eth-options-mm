"""Build Surface A: eSSVI refits every refit_interval_s over the study period.

    uv run python -m mm.surface.build --config config/base.yaml

Input: data/cache/option_trades_priced (mark IVs with each trade's own forward, D018).
Output: data/cache/surface_a/
    fits.parquet          one row per (refit_ts, expiry): ladder kind, eSSVI and raw SVI
                          parameters, fit error, butterfly minimum g
    refits.parquet        one row per refit_ts: ladder counts, arbitrage flags,
                          constant-maturity ATM / RR25 / BF25
    expiry_stats.parquet  per-expiry ATM / RR25 / BF25 every stats_interval_s

Within a refit, expiries are fitted short -> long (each is calendar-constrained by the
previous one). Parallelism is across fixed time chunks (chunk_days, plus a warm-up of
window + max_carry so carried state is rebuilt), so results do not depend on n_workers.
No lookahead: a refit at R only sees marks with R - window <= ts < R.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from mm.config import Config, SurfaceConfig, load_config
from mm.pricing.black76 import vega
from mm.pricing.iv import Status
from mm.pricing.time import MS_PER_YEAR
from mm.surface.arbitrage import butterfly_min, calendar_violations
from mm.surface.fit import Fit, _violates, fit_expiry, to_moneyness
from mm.surface.interp import total_variance_interp
from mm.surface.stats import smile_stats

log = logging.getLogger(__name__)
CACHE = Path("data/cache/surface_a")
RAW = ["a", "b", "rho", "m", "s"]


@dataclass
class BuildResult:
    fits: pd.DataFrame
    refits: pd.DataFrame
    expiry_stats: pd.DataFrame


def _ms(s) -> np.ndarray:
    return pd.Series(s).astype("datetime64[ms, UTC]").astype("int64").to_numpy()


def prepare_marks(priced: pd.DataFrame, cfg: SurfaceConfig) -> pd.DataFrame:
    """Filter usable mark IVs and add k (own forward) and the vega weight."""
    ok = (
        (priced["mark_iv_status"] == Status.OK)
        & (priced["mark_price_eth"] >= cfg.min_mark_price_eth)
        & (priced["mark_iv"] <= cfg.max_mark_iv)
        & (priced["t_years"] * 365 * 24 >= cfg.min_fit_hours)
        & priced["forward_usd"].notna()
    )
    m = priced.loc[ok, ["ts", "expiry", "strike_usd", "forward_usd", "t_years", "mark_iv"]].copy()
    m["k"] = to_moneyness(m)
    f = m["forward_usd"].to_numpy()
    # vega in ETH per vol point: weights comparable across time and spot levels
    m["vega"] = (
        np.asarray(
            vega(f, m["strike_usd"].to_numpy(), m["t_years"].to_numpy(), m["mark_iv"].to_numpy())
        )
        / f
    )
    m["ts_ms"] = _ms(m["ts"])
    m["exp_ms"] = _ms(m["expiry"])
    return m.sort_values("ts", kind="stable").reset_index(drop=True)


def recency_weights(ts_ms: np.ndarray, refit_ms: int, tau_s: float) -> np.ndarray:
    """exp(-age / tau): within the window, recent marks dominate the level while older
    marks still help pin the shape (decision D024)."""
    return np.exp(-(refit_ms - np.asarray(ts_ms, dtype=float)) / (1000.0 * tau_s))


def _row(r, exp_ms, fit: Fit, kgrid):
    e, p = fit.essvi, fit.params
    return {
        "refit_ts": r,
        "expiry": exp_ms,
        "t_ref": fit.t_ref,
        "kind": fit.kind,
        "theta": e.theta if e else np.nan,
        "rho_e": e.rho if e else np.nan,
        "psi": e.psi if e else np.nan,
        **(dict(zip(RAW, p.as_tuple(), strict=True)) if p else dict.fromkeys(RAW, np.nan)),
        "n_obs": fit.n_obs,
        "n_strikes": fit.n_strikes,
        "rmse_volpts": fit.rmse_volpts,
        "note": fit.note,
        "butterfly_min_g": butterfly_min(p, kgrid) if p else np.nan,
    }


def _refit_record(r, fits_now: list[tuple[float, Fit]], sc: SurfaceConfig, kgrid):
    live = [(t, f) for t, f in fits_now if f.kind != "none"]
    nodes = [(t, f.params) for t, f in live]
    cal = calendar_violations(nodes, kgrid, sc.calendar_tol) if len(nodes) > 1 else []
    kinds = [f.kind for _, f in fits_now]
    rec = {
        "refit_ts": r,
        **{f"n_{k}": kinds.count(k) for k in ("full", "carry", "none")},
        "any_butterfly": any(butterfly_min(f.params, kgrid) < -sc.butterfly_tol for _, f in live),
        "n_calendar": len(cal),
        "any_calendar": bool(cal),
        "atm_cm": np.nan,
        "rr25_cm": np.nan,
        "bf25_cm": np.nan,
    }
    t_cm = sc.const_maturity_days / 365
    if nodes and min(n[0] for n in nodes) <= t_cm <= max(n[0] for n in nodes):

        def vol(k):
            w = total_variance_interp(nodes, np.atleast_1d(k), t_cm)
            return np.sqrt(np.maximum(w, 0.0) / t_cm)

        try:
            st = smile_stats(vol, t_cm)
            rec |= {"atm_cm": st["atm"], "rr25_cm": st["rr25"], "bf25_cm": st["bf25"]}
        except ValueError:
            pass
    return rec


def _run_chunk(args):
    marks, refit_ms, emit_from, sc, kgrid = args
    groups = {
        int(e): (g["ts_ms"].to_numpy(), g.reset_index(drop=True))
        for e, g in marks.groupby("exp_ms")
    }
    first = {e: ts[0] for e, (ts, _) in groups.items()}
    state: dict[int, dict] = {e: {"prev": None, "last_data": None, "bounds": None} for e in groups}
    win = sc.trailing_window_s * 1000
    fit_rows, refit_rows, stat_rows = [], [], []
    for r in refit_ms:
        active = sorted(e for e in groups if first[e] < r and e - r >= sc.min_fit_hours * 3_600_000)
        shorter, fits_now = None, []
        for e in active:
            ts, obs = groups[e]
            st = state[e]
            t_ref = (e - r) / MS_PER_YEAR
            lo = int(np.searchsorted(ts, r - win, side="left"))
            hi = int(np.searchsorted(ts, r, side="left"))  # strictly before r
            prev = st["prev"]
            fit = None
            if (lo, hi) == st["bounds"] and prev is not None and prev.kind == "full":
                e_scaled = prev.essvi.scaled(t_ref / prev.t_ref)
                if not _violates(e_scaled, shorter, obs["k"].to_numpy()[lo:hi], sc):
                    fit = Fit("full", e_scaled, t_ref, prev.n_obs, prev.n_strikes, prev.rmse_volpts)
            if fit is None:
                age = (r - st["last_data"]) / 1000 if st["last_data"] is not None else np.inf
                win_obs = obs.iloc[lo:hi]
                win_obs = win_obs.assign(
                    vega=win_obs["vega"].to_numpy()
                    * recency_weights(win_obs["ts_ms"].to_numpy(), r, sc.recency_tau_s)
                )
                fit = fit_expiry(win_obs, prev, sc, t_ref=t_ref, prev_age_s=age, shorter=shorter)
            st["bounds"] = (lo, hi)
            if fit.kind == "full":
                st["last_data"] = r
            skip = fit.kind == "none" and hi == lo and prev is not None and prev.kind == "none"
            st["prev"] = fit
            if fit.kind != "none":
                shorter = fit.essvi
            if skip:
                continue
            fits_now.append((t_ref, fit))
            if r >= emit_from:
                fit_rows.append(_row(r, e, fit, kgrid))
                if r % (sc.stats_interval_s * 1000) == 0 and fit.kind != "none":
                    with contextlib.suppress(ValueError):
                        stat_rows.append(
                            {
                                "refit_ts": r,
                                "expiry": e,
                                "t_ref": t_ref,
                                "kind": fit.kind,
                                **smile_stats(fit.params, t_ref),
                            }
                        )
        if r >= emit_from:
            refit_rows.append(_refit_record(r, fits_now, sc, kgrid))
    return fit_rows, refit_rows, stat_rows


def _refit_grid(cfg: Config) -> np.ndarray:
    s = pd.Timestamp(cfg.study_period.start, tz="UTC")
    e = pd.Timestamp(cfg.study_period.end, tz="UTC")
    step = pd.Timedelta(seconds=cfg.surface.refit_interval_s)
    return _ms(pd.Series(pd.date_range(s + step, e, freq=step, inclusive="both")))


def build_refits(priced: pd.DataFrame, cfg: Config, refit_times=None) -> BuildResult:
    sc = cfg.surface
    marks = prepare_marks(priced, sc)
    refit_ms = _ms(pd.Series(list(refit_times))) if refit_times is not None else _refit_grid(cfg)
    kgrid = np.linspace(sc.k_grid[0], sc.k_grid[1], int(sc.k_grid[2]))
    origin = int(pd.Timestamp(cfg.study_period.start, tz="UTC").value // 10**6)
    chunk = int(sc.chunk_days * 86_400_000)
    warm = int((sc.trailing_window_s + sc.max_carry_s) * 1000)
    step = sc.refit_interval_s * 1000
    tasks = []
    for c in np.unique((refit_ms - origin) // chunk):
        c0, c1 = origin + c * chunk, origin + (c + 1) * chunk
        emit = refit_ms[(refit_ms >= c0) & (refit_ms < c1)]
        warm_grid = np.arange(emit[0] - warm, emit[0], step) if len(emit) else np.array([], int)
        grid = np.concatenate([warm_grid, emit]).astype("int64")
        sel = (marks["ts_ms"] >= grid[0] - sc.trailing_window_s * 1000) & (
            marks["ts_ms"] < grid[-1]
        )
        tasks.append((marks[sel], grid, int(emit[0]), sc, kgrid))
    if sc.n_workers > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(max_workers=sc.n_workers) as ex:
            results = list(ex.map(_run_chunk, tasks, chunksize=1))
    else:
        results = [_run_chunk(t) for t in tasks]
    fits = pd.DataFrame([x for res in results for x in res[0]])
    refits = pd.DataFrame([x for res in results for x in res[1]])
    stats = pd.DataFrame([x for res in results for x in res[2]])
    for df in (fits, refits, stats):
        for col in ("refit_ts", "expiry"):
            if col in df:
                df[col] = pd.to_datetime(df[col], unit="ms", utc=True).astype("datetime64[ms, UTC]")
    if len(fits):
        fits = fits.sort_values(["refit_ts", "t_ref"], kind="stable").reset_index(drop=True)
    return BuildResult(fits, refits, stats)


def main(argv: list[str] | None = None) -> None:
    import duckdb

    p = argparse.ArgumentParser(prog="mm.surface.build", description=__doc__)
    p.add_argument("--config", required=True)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    con = duckdb.connect()
    con.execute("set TimeZone = 'UTC'")
    glob = "data/cache/option_trades_priced/date=*/*.parquet"
    priced = con.sql(
        "select ts, expiry, strike_usd, option_type, forward_usd, t_years, mark_iv, "
        f"mark_iv_status, mark_price_eth from read_parquet('{glob}', hive_partitioning=true)"
    ).df()
    log.info("priced rows %d", len(priced))
    res = build_refits(priced, cfg)
    CACHE.mkdir(parents=True, exist_ok=True)
    for name, df in (
        ("fits", res.fits),
        ("refits", res.refits),
        ("expiry_stats", res.expiry_stats),
    ):
        df.to_parquet(CACHE / f"{name}.parquet", index=False, compression="zstd")
        log.info("%s: %d rows", name, len(df))


if __name__ == "__main__":
    main()
