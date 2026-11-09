"""Build Surface A: SVI refits every refit_interval_s over the study period.

    uv run python -m mm.surface.build --config config/base.yaml

Input: data/cache/option_trades_priced (mark IVs with each trade's own forward, D018).
Output: data/cache/surface_a/
    fits.parquet          one row per (refit_ts, expiry): ladder kind, SVI params, fit error
    refits.parquet        one row per refit_ts: ladder counts, arbitrage flags,
                          constant-maturity ATM / RR25 / BF25
    expiry_stats.parquet  per-expiry ATM / RR25 / BF25 every stats_interval_s

No lookahead: a refit at R only sees marks with R - window <= ts < R.
"""

from __future__ import annotations

import argparse
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
from mm.surface.fit import Fit, fit_expiry, to_moneyness
from mm.surface.interp import total_variance_interp
from mm.surface.stats import smile_stats
from mm.surface.svi import SVI

log = logging.getLogger(__name__)
CACHE = Path("data/cache/surface_a")
FIT_COLS = ["a", "b", "rho", "m", "s"]


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
    # vega in ETH per vol point: makes weights comparable across time and spot levels
    m["vega"] = (
        np.asarray(
            vega(f, m["strike_usd"].to_numpy(), m["t_years"].to_numpy(), m["mark_iv"].to_numpy())
        )
        / f
    )
    return m.sort_values("ts", kind="stable").reset_index(drop=True)


def _fit_one_expiry(args) -> list[dict]:
    exp_ms, obs, refit_ms, cfg, kgrid = args
    ts = _ms(obs["ts"])
    win = cfg.trailing_window_s * 1000
    first = ts[0] if len(ts) else None
    rows, prev, last_data_ms = [], None, None
    last_bounds = None
    for r in refit_ms:
        if first is None or r <= first or exp_ms - r < cfg.min_fit_hours * 3_600_000:
            continue
        t_ref = (exp_ms - r) / MS_PER_YEAR
        lo = np.searchsorted(ts, r - win, side="left")
        hi = np.searchsorted(ts, r, side="left")  # strictly before r
        if (lo, hi) == last_bounds and prev is not None and prev.kind in ("full", "level"):
            fit = Fit(
                prev.kind,
                prev.params.scaled(t_ref / prev.t_ref),
                t_ref,
                prev.n_obs,
                prev.n_strikes,
                prev.rmse_volpts,
            )
        else:
            age = (r - last_data_ms) / 1000 if last_data_ms is not None else np.inf
            fit = fit_expiry(obs.iloc[lo:hi], prev, cfg, t_ref=t_ref, prev_age_s=age)
        last_bounds = (lo, hi)
        if fit.kind in ("full", "level"):
            last_data_ms = r
        if fit.kind == "none" and hi == lo and prev is not None and prev.kind == "none":
            prev = fit
            continue  # no data and nothing to carry: don't emit endless empty rows
        prev = fit
        p = fit.params
        rows.append(
            {
                "refit_ts": r,
                "expiry": exp_ms,
                "t_ref": t_ref,
                "kind": fit.kind,
                **(
                    dict(zip(FIT_COLS, p.as_tuple(), strict=True))
                    if p
                    else dict.fromkeys(FIT_COLS, np.nan)
                ),
                "n_obs": fit.n_obs,
                "n_strikes": fit.n_strikes,
                "rmse_volpts": fit.rmse_volpts,
                "butterfly_min_g": butterfly_min(p, kgrid) if p else np.nan,
            }
        )
    return rows


def _refit_grid(cfg: Config) -> list[pd.Timestamp]:
    s = pd.Timestamp(cfg.study_period.start, tz="UTC")
    e = pd.Timestamp(cfg.study_period.end, tz="UTC")
    return list(
        pd.date_range(
            s + pd.Timedelta(seconds=cfg.surface.refit_interval_s),
            e,
            freq=f"{cfg.surface.refit_interval_s}s",
            inclusive="both",
        )
    )


def build_refits(priced: pd.DataFrame, cfg: Config, refit_times=None) -> BuildResult:
    sc = cfg.surface
    marks = prepare_marks(priced, sc)
    refit_times = refit_times if refit_times is not None else _refit_grid(cfg)
    refit_ms = _ms(pd.Series(list(refit_times)))
    kgrid = np.linspace(sc.k_grid[0], sc.k_grid[1], int(sc.k_grid[2]))
    tasks = [
        (int(pd.Timestamp(e).value // 10**6), g.reset_index(drop=True), refit_ms, sc, kgrid)
        for e, g in marks.groupby("expiry")
    ]
    if sc.n_workers > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(max_workers=sc.n_workers) as ex:
            results = list(ex.map(_fit_one_expiry, tasks, chunksize=1))
    else:
        results = [_fit_one_expiry(t) for t in tasks]
    fits = pd.DataFrame([r for res in results for r in res])
    if fits.empty:
        empty = pd.DataFrame()
        return BuildResult(fits, empty, empty)
    fits = fits.sort_values(["refit_ts", "t_ref"], kind="stable").reset_index(drop=True)
    refits, exp_stats = _per_refit(fits, cfg, kgrid)
    for df in (fits, refits, exp_stats):
        if "refit_ts" in df:
            df["refit_ts"] = pd.to_datetime(df["refit_ts"], unit="ms", utc=True).astype(
                "datetime64[ms, UTC]"
            )
    fits["expiry"] = pd.to_datetime(fits["expiry"], unit="ms", utc=True).astype(
        "datetime64[ms, UTC]"
    )
    if len(exp_stats):
        exp_stats["expiry"] = pd.to_datetime(exp_stats["expiry"], unit="ms", utc=True).astype(
            "datetime64[ms, UTC]"
        )
    return BuildResult(fits, refits, exp_stats)


def _per_refit(fits: pd.DataFrame, cfg: Config, kgrid: np.ndarray):
    sc = cfg.surface
    t_cm = sc.const_maturity_days / 365
    rows, stats_rows = [], []
    for r, g in fits.groupby("refit_ts", sort=True):
        live = g[g["kind"] != "none"]
        nodes = [(row.t_ref, SVI(row.a, row.b, row.rho, row.m, row.s)) for row in live.itertuples()]
        cal = calendar_violations(nodes, kgrid, sc.calendar_tol) if len(nodes) > 1 else []
        counts = g["kind"].value_counts()
        rec = {
            "refit_ts": r,
            **{f"n_{k}": int(counts.get(k, 0)) for k in ("full", "level", "carry", "none")},
            "any_butterfly": bool((live["butterfly_min_g"] < -sc.butterfly_tol).any()),
            "n_calendar": len(cal),
            "any_calendar": bool(cal),
            "atm_cm": np.nan,
            "rr25_cm": np.nan,
            "bf25_cm": np.nan,
        }
        # constant maturity: only if bracketed by fitted expiries (no extrapolation)
        if nodes and min(n[0] for n in nodes) <= t_cm <= max(n[0] for n in nodes):

            def vol(k, nodes=nodes):
                return np.sqrt(
                    np.maximum(total_variance_interp(nodes, np.atleast_1d(k), t_cm), 0) / t_cm
                )

            try:
                st = smile_stats(vol, t_cm)
                rec |= {"atm_cm": st["atm"], "rr25_cm": st["rr25"], "bf25_cm": st["bf25"]}
            except ValueError:
                pass
        rows.append(rec)
        if r % (sc.stats_interval_s * 1000) == 0:
            for row, (t, p) in zip(live.itertuples(), nodes, strict=True):
                try:
                    st = smile_stats(p, t)
                except ValueError:
                    continue
                stats_rows.append(
                    {"refit_ts": r, "expiry": row.expiry, "t_ref": t, "kind": row.kind, **st}
                )
    return pd.DataFrame(rows), pd.DataFrame(stats_rows)


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
