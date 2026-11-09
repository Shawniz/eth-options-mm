"""Phase 3 report: Surface A fit quality, arbitrage and DVOL tracking.

    uv run python -m mm.surface.report --config config/base.yaml

Reads data/cache/surface_a (mm.surface.build) and data/clean/dvol. Writes
reports/phase3_surface.md and reports/figures/phase3_*.png. Exit code 1 on FAIL.

'Normal days' are days whose |daily DVOL change| is below the quantile set in config,
with the quantile measured on the TRAINING period only (CLAUDE.md time split).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from mm.config import Config, load_config  # noqa: E402
from mm.data.db import connect  # noqa: E402
from mm.surface.build import CACHE  # noqa: E402

SURFACE, INK, INK2, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
T_BUCKETS = [0, 2, 8, 35, 100, 10000]


def _style(ax, title):
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=10, color=INK, pad=6)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=INK2, labelsize=8, length=0)


def _md(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        cells = []
        for v in r.tolist():
            if isinstance(v, float):
                cells.append(f"{v:.3f}")
            elif isinstance(v, (int, np.integer)):
                cells.append(f"{v:,}")
            else:
                cells.append(str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def load(cfg: Config):
    fits = pd.read_parquet(CACHE / "fits.parquet")
    refits = pd.read_parquet(CACHE / "refits.parquet")
    dvol = (
        connect(cfg.data.clean_dir)
        .execute("select ts, available_at, dvol_close from dvol order by ts")
        .df()
    )
    for df, cols in (
        (fits, ["refit_ts", "expiry"]),
        (refits, ["refit_ts"]),
        (dvol, ["ts", "available_at"]),
    ):
        for c in cols:
            df[c] = pd.to_datetime(df[c], utc=True)
    return fits, refits, dvol


def rmse_table(fits: pd.DataFrame, cfg: Config) -> tuple[pd.DataFrame, bool]:
    full = fits[fits["kind"] == "full"].copy()
    days = full["t_ref"] * 365
    rows, ok_all = [], True
    for tg in cfg.surface.validation.rmse_targets:
        x = full.loc[(days > tg.min_days) & (days <= tg.max_days), "rmse_volpts"]
        med, p95 = float(x.median()), float(x.quantile(0.95))
        ok = med <= tg.median and p95 <= tg.p95
        ok_all &= ok
        rows.append(
            {
                "expiry days": f"({tg.min_days:g}, {tg.max_days:g}]",
                "fits": len(x),
                "median": med,
                "p95": p95,
                "target median": tg.median,
                "target p95": tg.p95,
                "pass": "✅" if ok else "❌",
            }
        )
    return pd.DataFrame(rows), ok_all


def bucket_table(fits: pd.DataFrame) -> pd.DataFrame:
    f = fits.copy()
    f["T"] = pd.cut(f["t_ref"] * 365, T_BUCKETS).astype(str)
    full = f[f["kind"] == "full"]
    a = full.groupby("T")["rmse_volpts"].agg(
        full_fits="size", median="median", p95=lambda x: x.quantile(0.95)
    )
    k = f.groupby("T")["kind"].value_counts(normalize=True).unstack(fill_value=0)
    k.columns = [f"share {c}" for c in k.columns]
    return a.join(k).reset_index().rename(columns={"T": "expiry days"})


def normal_days(dvol: pd.DataFrame, cfg: Config) -> pd.Series:
    d = dvol.set_index("ts")["dvol_close"].resample("1D").last()
    chg = d.diff().abs()
    train = chg[chg.index < pd.Timestamp(cfg.study_period.train_end, tz="UTC")]
    thr = float(train.quantile(cfg.surface.validation.normal_day_dvol_quantile))
    return (chg < thr).rename("normal"), thr


def dvol_tracking(refits: pd.DataFrame, dvol: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    # Each hourly DVOL close becomes known at available_at (end of its hour). Compare with
    # the 30-day ATM at that same refit time.
    r = refits.set_index("refit_ts")["atm_cm"]
    d = dvol.set_index("available_at")["dvol_close"]
    j = pd.concat([r.rename("atm"), d.rename("dvol")], axis=1, join="inner").dropna()
    corr = float(j.diff().dropna().corr().iloc[0, 1])
    return j, corr


def plot_atm_dvol(j: pd.DataFrame, refits: pd.DataFrame, path: Path) -> None:
    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True, facecolor=SURFACE)
    axes[0].plot(j.index, j["dvol"] * 100, color=SERIES[1], linewidth=1.2, label="DVOL")
    axes[0].plot(
        j.index, j["atm"] * 100, color=SERIES[0], linewidth=1.2, label="30-day ATM (Surface A)"
    )
    _style(axes[0], "30-day ATM implied vol vs DVOL (vol points, hourly)")
    axes[0].legend(frameon=False, fontsize=8, loc="upper left", labelcolor=INK2)
    h = refits.set_index("refit_ts")[["rr25_cm", "bf25_cm"]].resample("1h").last()
    axes[1].plot(h.index, h["rr25_cm"] * 100, color=SERIES[0], linewidth=1.0)
    axes[1].axhline(0, color=AXIS, linewidth=0.8)
    _style(axes[1], "30-day 25-delta risk reversal (vol points; > 0 means calls richer)")
    axes[2].plot(h.index, h["bf25_cm"] * 100, color=SERIES[0], linewidth=1.0)
    _style(axes[2], "30-day 25-delta butterfly (vol points)")
    axes[2].xaxis.set_major_locator(mdates.MonthLocator())
    axes[2].xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    fig.tight_layout(h_pad=1.5)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def write_report(cfg: Config, out: Path) -> bool:
    v = cfg.surface.validation
    fits, refits, dvol = load(cfg)
    rmse_tbl, rmse_ok = rmse_table(fits, cfg)
    normal, thr = normal_days(dvol, cfg)
    rf = refits.assign(day=refits["refit_ts"].dt.floor("D"))
    rf["clean"] = ~(rf["any_butterfly"] | rf["any_calendar"])
    rf = rf.join(normal, on="day")
    on_normal = rf[rf["normal"].fillna(False)]
    clean_frac = float(on_normal["clean"].mean())
    arb_ok = clean_frac >= v.target_arb_free_frac
    j, corr = dvol_tracking(refits, dvol)
    corr_ok = corr >= v.target_dvol_change_corr
    spread = (j["dvol"] - j["atm"]) * 100
    passed = rmse_ok and arb_ok and corr_ok
    plot_atm_dvol(j, refits, Path("reports/figures/phase3_atm_vs_dvol.png"))
    worst = (
        rf.groupby("day")["clean"]
        .mean()
        .sort_values()
        .head(8)
        .reset_index()
        .rename(columns={"clean": "share clean"})
    )
    worst["day"] = worst["day"].dt.strftime("%Y-%m-%d")
    mark = {True: "✅", False: "❌"}
    arb_row = (
        f"| arbitrage-free refits on normal days | {clean_frac * 100:.1f}% "
        f"| ≥ {v.target_arb_free_frac * 100:.0f}% | {mark[arb_ok]} |"
    )
    corr_row = (
        f"| 30d ATM vs DVOL, hourly-change correlation | {corr:.3f} "
        f"| ≥ {v.target_dvol_change_corr} | {mark[corr_ok]} |"
    )
    md = f"""# Phase 3: Surface A (eSSVI on Deribit mark IVs)

Generated by `uv run python -m mm.surface.report --config config/base.yaml`.
{len(refits):,} refits every {cfg.surface.refit_interval_s} s; {len(fits):,} expiry fits.

## Result: {"PASS" if passed else "FAIL"}

| check | result | target | |
|---|---|---|---|
| fit error | see table below | per expiry bucket | {"✅" if rmse_ok else "❌"} |
{arb_row}
{corr_row}

Normal day: |daily DVOL change| < {thr * 100:.2f} vol points (the {v.normal_day_dvol_quantile:.0%}
quantile of the training period, up to {cfg.study_period.train_end}).
DVOL − 30d ATM: mean {spread.mean():.2f}, std {spread.std():.2f} vol points (DVOL is a
variance-swap-style index, so it sits above ATM by construction).

## Fit error (vega- and recency-weighted RMSE of full fits, vol points)

{_md(rmse_tbl)}

## By expiry: fit error and ladder usage

{_md(bucket_table(fits))}

## Arbitrage

- Refits with a butterfly flag: {refits["any_butterfly"].mean() * 100:.2f}% (all days)
- Refits with a calendar flag: {refits["any_calendar"].mean() * 100:.2f}% (all days)

Days with the lowest share of clean refits:

{_md(worst)}

![ATM vs DVOL](figures/phase3_atm_vs_dvol.png)
"""
    out.write_text(md)
    return passed


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="mm.surface.report", description=__doc__)
    p.add_argument("--config", required=True)
    a = p.parse_args(argv)
    ok = write_report(load_config(a.config), Path("reports/phase3_surface.md"))
    print("PASS" if ok else "FAIL", "-> reports/phase3_surface.md")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
