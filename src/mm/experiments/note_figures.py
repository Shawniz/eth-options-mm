"""Phase 9 research-note figures, from the Phase 8 runner's saved outputs (no rerun).

    uv run python -m mm.experiments.note_figures --config config/base.yaml \
        --experiments config/experiments.yaml

Also called at the end of `mm.run --experiments`. For the "best" variant on the test
period it writes reports/figures/:
  note_cumulative.png  cumulative P&L (baseline for comparison) and daily components
  note_markouts.png    delta-hedged markout vs horizon, by expiry and by OTM delta
  note_worst_day.png   the worst test day: ETH index with our fills and hedges, the vega
                       we traded, and the day's P&L by component (daily totals; the run
                       keeps no intraday attribution, D045 / Phase 9 option B)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from mm.config import load_config  # noqa: E402
from mm.pnl.attribution import COMPONENTS  # noqa: E402
from mm.pnl.report import (  # noqa: E402
    AXIS,
    GROUPS,
    INK,
    INK2,
    SERIES,
    SURFACE,
    _style,
    fig_cumulative,
    fig_markouts,
)

DAY_MS = 86_400_000


def worst_day(daily: pd.DataFrame) -> pd.Timestamp:
    """The UTC day with the lowest total P&L."""
    return pd.Timestamp(daily["total"].idxmin()).tz_localize("UTC")


def daily_rows(daily: pd.DataFrame) -> pd.DataFrame:
    """Daily component table -> attribution-style rows (one per day, stamped at noon) so
    the Phase 7 figure code can draw it."""
    ts = pd.to_datetime(pd.Index(daily.index)).tz_localize("UTC") + pd.Timedelta("12h")
    rows = daily[COMPONENTS].reset_index(drop=True)
    rows.insert(0, "ts_ms", ts.as_unit("ms").asi8.astype("int64"))
    return rows


def traded(fills: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    """Our fills on one day with signed contracts, vega and cash gamma (bought > 0)."""
    t0 = int(day.value // 10**6)
    f = fills[(fills["ts_ms"] >= t0) & (fills["ts_ms"] < t0 + DAY_MS)].copy()
    sign = np.where(f["our_side"] == "buy", 1.0, -1.0)
    f["n"] = sign * f["qty"]
    f["vega_traded"] = f["n"] * f["vega_usd"]
    f["gamma_traded"] = f["n"] * f["gamma1_usd"]
    return f.sort_values("ts_ms", kind="stable")


def price_at(index: pd.Series, ts_ms: np.ndarray) -> np.ndarray:
    """Last index value at or before each time (ms), for placing fill markers."""
    t = index.index.as_unit("ms").asi8
    j = np.clip(np.searchsorted(t, ts_ms, side="right") - 1, 0, None)
    return index.to_numpy()[j]


def fig_day(fills, hedges, index: pd.Series, comps: pd.Series, day: pd.Timestamp, path: Path):
    """Left: ETH index with fills (▲ we bought, ▼ we sold) and hedge executions; below it
    the cumulative vega we traded. Right: the day's P&L by component."""
    f = traded(fills, day)
    t0 = int(day.value // 10**6)
    h = hedges[(hedges["exec_ms"] >= t0) & (hedges["exec_ms"] < t0 + DAY_MS)]
    fig = plt.figure(figsize=(11, 6.5), facecolor=SURFACE)
    gs = fig.add_gridspec(2, 2, width_ratios=[2.2, 1], height_ratios=[1.6, 1])
    a0 = fig.add_subplot(gs[0, 0])
    a1 = fig.add_subplot(gs[1, 0], sharex=a0)
    a2 = fig.add_subplot(gs[:, 1])

    a0.plot(index.index, index.to_numpy(), color=INK2, linewidth=1.0, label="ETH index")
    ft = pd.to_datetime(f["ts_ms"], unit="ms", utc=True)
    px = price_at(index, f["ts_ms"].to_numpy("int64"))
    for side, mk, col, lab in (("buy", "^", SERIES[0], "we bought options"),
                               ("sell", "v", SERIES[1], "we sold options")):  # fmt: skip
        m = (f["our_side"] == side).to_numpy()
        a0.scatter(ft[m], px[m], marker=mk, s=18, color=col, edgecolors=SURFACE,
                   linewidths=0.6, label=f"{lab} ({int(m.sum())})", zorder=3)  # fmt: skip
    ht = pd.to_datetime(h["exec_ms"], unit="ms", utc=True)
    a0.scatter(ht, h["exec_usd"], marker="|", s=40, color=SERIES[2],
               label=f"perp hedges ({len(h)})", zorder=2)  # fmt: skip
    a0.legend(frameon=False, fontsize=8, loc="best", labelcolor=INK2)
    _style(a0, f"Worst test day {day:%d %b %Y}: ETH index (USD), fills and hedges")

    cum = f["vega_traded"].cumsum().to_numpy()
    a1.step(ft, cum, where="post", color=SERIES[0], linewidth=1.4)
    a1.axhline(0, color=AXIS, linewidth=0.8)
    _style(a1, "Cumulative vega traded that day, USD per vol point (bought > 0)")
    a1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

    c = pd.concat([comps, pd.Series({"total": comps.sum()})])
    names = list(c.index)[::-1]
    vals = c.to_numpy()[::-1] / 1e3
    cols = [SERIES[0] if v >= 0 else SERIES[1] for v in vals]
    cols[0] = INK  # the total
    a2.barh(names, vals, color=cols, height=0.6, edgecolor=SURFACE, linewidth=0.8)
    a2.axvline(0, color=AXIS, linewidth=0.8)
    for y, v in enumerate(vals):
        a2.text(v, y, f" {v:+.1f} ", va="center", ha="left" if v >= 0 else "right",
                fontsize=8, color=INK)  # fmt: skip
    _style(a2, "That day's P&L, $ thousands")
    a2.grid(axis="y", visible=False)
    a2.grid(axis="x", color="#e1e0d9", linewidth=0.6)
    lo, hi = min(vals.min(), 0), max(vals.max(), 0)
    pad = 0.25 * (hi - lo or 1)
    a2.set_xlim(lo - pad, hi + pad)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def grouped_day(daily: pd.DataFrame, day: pd.Timestamp) -> pd.Series:
    r = daily.loc[day.date()]
    return pd.Series({k: float(r[v].sum()) for k, v in GROUPS.items()})


def index_series(cfg, day: pd.Timestamp, step="1min") -> pd.Series:
    from mm.pricing.enrich import build_curve

    c = build_curve(cfg, day.date(), (day + pd.Timedelta("1D")).date())
    t = pd.date_range(day, day + pd.Timedelta("1D"), freq=step, inclusive="left")
    return pd.Series([c.index_at(int(x.value // 10**6), inclusive=True) for x in t], index=t)


def write(base_path: str, exp_path: str, out_dir: Path = Path("reports/figures")) -> list[Path]:
    cfg = load_config(base_path)
    exp = yaml.safe_load(Path(exp_path).read_text())
    root = Path(exp["settings"]["out_dir"]) / "test"
    meta = json.loads((Path(exp["settings"]["out_dir"]) / "meta.json").read_text())
    name = "best" if "best" in meta["variants"] else "baseline"
    run = root / name
    daily = pd.read_parquet(run / "daily_pnl.parquet")
    base = pd.read_parquet(root / "baseline" / "daily_pnl.parquet")
    out_dir.mkdir(parents=True, exist_ok=True)
    p_cum, p_mk, p_day = (out_dir / f"note_{x}.png" for x in ("cumulative", "markouts",
                                                              "worst_day"))  # fmt: skip
    fig_cumulative(daily_rows(daily), p_cum, ref=base["total"], ref_label="baseline",
                   label=name)  # fmt: skip
    fig_markouts(pd.read_parquet(run / "markouts.parquet"), p_mk)
    day = worst_day(daily)
    fig_day(pd.read_parquet(run / "fills.parquet"), pd.read_parquet(run / "hedges.parquet"),
            index_series(cfg, day), grouped_day(daily, day), day, p_day)  # fmt: skip
    return [p_cum, p_mk, p_day]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="mm.experiments.note_figures")
    p.add_argument("--config", required=True)
    p.add_argument("--experiments", required=True)
    a = p.parse_args(argv)
    for x in write(a.config, a.experiments):
        print(x)


if __name__ == "__main__":
    main()
