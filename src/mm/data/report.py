"""Phase 1 data report: completeness, gaps and filter counts.

    uv run python -m mm.data.cli report --config config/base.yaml

Writes reports/figures/phase1_daily.png and reports/phase1_data.md. Every number in the
markdown comes from data/clean via DuckDB, so the report is reproducible.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from mm.config import Config  # noqa: E402
from mm.data.db import connect  # noqa: E402

# Reference palette (dataviz skill, light mode)
SURFACE, INK, INK2, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]


def daily_stats(cfg: Config) -> pd.DataFrame:
    con = connect(cfg.data.clean_dir)
    s, e = str(cfg.study_period.start), str(cfg.study_period.end)
    q = f"""
    with days as (
        select unnest(range(date '{s}', date '{e}', interval 1 day))::date as day
    ),
    opt as (
        select date::date day, count(*) n_opt, sum(is_suspect::int) n_suspect,
               sum(amount_contracts) opt_contracts
        from option_trades group by 1
    ),
    opt_gap as (
        select day, max(gap_s) max_gap_opt_s from (
            select date::date day,
                   epoch(ts - lag(ts) over (partition by date order by ts)) gap_s
            from option_trades) group by 1
    ),
    perp as (select date::date day, count(*) n_perp from perp_trades group by 1),
    perp_gap as (
        select day, max(gap_s) max_gap_perp_s from (
            select date::date day,
                   epoch(ts - lag(ts) over (partition by date order by ts)) gap_s
            from perp_trades) group by 1
    ),
    fut as (select date::date day, count(*) n_fut from future_trades group by 1),
    dv as (select date::date day, count(*) n_dvol, last(dvol_close order by ts) dvol
           from dvol group by 1),
    fu as (select date::date day, count(*) n_funding, sum(interest_1h) funding_day
           from funding group by 1)
    select * from days
    left join opt using (day) left join opt_gap using (day)
    left join perp using (day) left join perp_gap using (day)
    left join fut using (day) left join dv using (day) left join fu using (day)
    order by day
    """
    df = con.execute(q).df()
    df["day"] = pd.to_datetime(df["day"])
    # filter counts from the per-day clean reports
    rows = []
    for f in sorted((Path(cfg.data.clean_dir) / "_reports" / "option_trades").glob("*.json")):
        r = json.loads(f.read_text())
        rows.append(
            {"day": pd.Timestamp(r["date"]), **{f"drop_{k}": v for k, v in r["dropped"].items()}}
        )
    if rows:
        df = df.merge(pd.DataFrame(rows), on="day", how="left")
    return df.fillna({c: 0 for c in df.columns if c.startswith(("n_", "drop_"))})


def _style(ax, title: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=10, color=INK, pad=6)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=INK2, labelsize=8, length=0)


def plot_daily(df: pd.DataFrame, path: Path) -> Path:
    fig, axes = plt.subplots(4, 1, figsize=(10, 9), sharex=True, facecolor=SURFACE)
    w = 0.8
    axes[0].bar(df["day"], df["n_opt"] / 1e3, width=w, color=SERIES[0])
    _style(axes[0], "Option trades per day (thousands, after filters)")
    axes[1].bar(df["day"], df["n_perp"] / 1e3, width=w, color=SERIES[0])
    _style(axes[1], "ETH-PERPETUAL trades per day (thousands)")
    axes[2].plot(df["day"], df["dvol"] * 100, color=SERIES[0], linewidth=2)
    _style(axes[2], "DVOL, daily close (vol points)")
    bottom = 0
    for i, (col, label) in enumerate(
        [("drop_block", "block"), ("drop_combo", "combo"), ("drop_invalid", "invalid")]
    ):
        if col in df:
            axes[3].bar(
                df["day"],
                df[col],
                width=w,
                bottom=bottom,
                color=SERIES[i],
                label=label,
                edgecolor=SURFACE,
                linewidth=0.5,
            )
            bottom = bottom + df[col]
    _style(axes[3], "Option trades removed per day, by filter")
    axes[3].legend(frameon=False, fontsize=8, ncol=3, loc="upper left", labelcolor=INK2)
    axes[3].xaxis.set_major_locator(mdates.MonthLocator())
    axes[3].xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    fig.tight_layout(h_pad=1.5)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return path


def _md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(str(v) for v in r.tolist()) + " |")
    return "\n".join(lines)


def write_report(cfg: Config, out_md: Path, out_png: Path) -> pd.DataFrame:
    df = daily_stats(cfg)
    plot_daily(df, out_png)
    d = df.assign(date=df["day"].dt.strftime("%Y-%m-%d"), weekday=df["day"].dt.strftime("%a"))
    med_opt, med_perp = df["n_opt"].median(), df["n_perp"].median()
    low = d[(d["n_opt"] < 0.4 * med_opt) | (d["n_perp"] < 0.4 * med_perp)]
    incomplete = d[(d["n_dvol"] != 24) | (d["n_funding"] != 24)]
    gaps_opt = d.nlargest(5, "max_gap_opt_s")
    gaps_perp = d.nlargest(5, "max_gap_perp_s")
    zero = d[(d["n_opt"] == 0) | (d["n_perp"] == 0) | (d["n_fut"] == 0)]
    tot = {c: int(df[c].sum()) for c in df.columns if c.startswith("drop_") or c == "n_suspect"}

    def sel(x, cols):
        return _md_table(x[cols].round(1)) if len(x) else "_none_"

    md = f"""# Phase 1 data report

Generated by `uv run python -m mm.data.cli report --config config/base.yaml`. Do not edit
by hand; the interpretation is in `reports/phase1_data_notes.md`.

Study period {cfg.study_period.start} to {cfg.study_period.end} (end exclusive), {len(df)} UTC days.

![daily counts](figures/{out_png.name})

## Totals

| | value |
|---|---|
| option trades kept | {int(df["n_opt"].sum()):,} |
| of which flagged suspect | {int(df["n_suspect"].sum()):,} |
| perp trades | {int(df["n_perp"].sum()):,} |
| dated-future trades | {int(df["n_fut"].sum()):,} |
| option trades removed | {json.dumps({k: v for k, v in tot.items() if k != "n_suspect"})} |
| median option trades/day | {med_opt:,.0f} |
| median perp trades/day | {med_perp:,.0f} |

## Days with no trades in a stream

{sel(zero, ["date", "weekday", "n_opt", "n_perp", "n_fut"])}

## Days with DVOL or funding not exactly 24 hourly rows

{sel(incomplete, ["date", "weekday", "n_dvol", "n_funding"])}

## Low-activity days (below 40% of the median, options or perp)

{sel(low, ["date", "weekday", "n_opt", "n_perp", "dvol"])}

## Longest gap between consecutive trades within a day (seconds)

Options:

{sel(gaps_opt, ["date", "weekday", "n_opt", "max_gap_opt_s"])}

Perpetual:

{sel(gaps_perp, ["date", "weekday", "n_perp", "max_gap_perp_s"])}
"""
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(md)
    return df
