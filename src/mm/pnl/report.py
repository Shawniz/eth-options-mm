"""P&L attribution report for one run.

    uv run python -m mm.pnl.report --config config/base.yaml --run 2025-01-27_2025-02-10

Reads data/runs/<run>/ (fills, hedges, ledger, daily) and the market data, replays the
account, attributes P&L (attribution.py), computes markouts (markouts.py) and writes
reports/pnl_<run>.md plus three figures in reports/figures/. Saves attribution.parquet
and markouts.parquet in the run directory.
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

from mm.config import Config, load_config  # noqa: E402
from mm.pnl.attribution import COMPONENTS, attribute  # noqa: E402
from mm.pnl.markouts import HORIZONS, markout_table, markouts  # noqa: E402
from mm.pnl.value import MarketData  # noqa: E402
from mm.sim.load import load_inputs  # noqa: E402

SURFACE, INK, INK2, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"]
GROUPS = {
    "theta": ["theta"],
    "gamma": ["gamma"],
    "vega": ["vega"],
    "delta + basis": ["delta", "basis"],
    "spread capture": ["spread_capture"],
    "hedge cost + fees": ["hedge_cost", "option_fee", "perp_fee", "delivery_fee"],
    "funding + settlement": ["funding", "settlement"],
}


def _t(ms):
    return pd.to_datetime(pd.Series(ms), unit="ms", utc=True)


def _md(df: pd.DataFrame, fmt="{:,.0f}") -> str:
    cols = [str(c) for c in df.columns]
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        out.append("| " + " | ".join(fmt.format(v) if isinstance(v, (float, np.floating))
                                     else str(v) for v in r.tolist()) + " |")  # fmt: skip
    return "\n".join(out)


def _style(ax, title):
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=10, color=INK, pad=6)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=INK2, labelsize=8, length=0)


def grouped(rows: pd.DataFrame, freq: str) -> pd.DataFrame:
    r = rows.assign(t=_t(rows["ts_ms"]).to_numpy()).set_index("t")
    g = pd.DataFrame({k: r[v].sum(axis=1) for k, v in GROUPS.items()})
    return g.resample(freq).sum()


def fig_cumulative(rows, path: Path, ref: pd.Series | None = None, ref_label: str = "",
                   label: str = "this run"):  # fmt: skip
    """ref: optional daily total P&L (indexed by date) drawn as a comparison line."""
    d = grouped(rows, "1D")
    fig, ax = plt.subplots(2, 1, figsize=(10, 7), sharex=True, facecolor=SURFACE,
                           gridspec_kw={"height_ratios": [1, 1.4]})  # fmt: skip
    ax[0].plot(d.index, d.sum(axis=1).cumsum() / 1e3, color=INK, linewidth=1.4, label=label)
    if ref is not None:
        rt = pd.to_datetime(pd.Index(ref.index)).tz_localize("UTC")
        ax[0].plot(rt, ref.cumsum().to_numpy() / 1e3, color=SERIES[1], linewidth=1.2,
                   linestyle="--", label=ref_label)  # fmt: skip
        ax[0].legend(frameon=False, fontsize=8, loc="lower left", labelcolor=INK2)
    ax[0].axhline(0, color=AXIS, linewidth=0.8)
    _style(ax[0], "Cumulative P&L, $ thousands")
    pos, neg = np.zeros(len(d)), np.zeros(len(d))
    w = 0.8
    for i, c in enumerate(d.columns):
        v = d[c].to_numpy() / 1e3
        bottom = np.where(v >= 0, pos, neg)
        ax[1].bar(d.index, v, w, bottom=bottom, color=SERIES[i], label=c,
                  edgecolor=SURFACE, linewidth=0.8)  # fmt: skip
        pos, neg = pos + np.maximum(v, 0), neg + np.minimum(v, 0)
    ax[1].axhline(0, color=AXIS, linewidth=0.8)
    _style(ax[1], "Daily P&L by component, $ thousands")
    ax[1].legend(frameon=False, fontsize=8, ncol=4, loc="lower left", labelcolor=INK2)
    ax[1].xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def fig_markouts(mk, path: Path):
    labs = list(HORIZONS)
    xs = list(range(len(labs)))  # evenly spaced horizons
    fig, ax = plt.subplots(1, 2, figsize=(10, 4), sharey=True, facecolor=SURFACE)
    for a, by, title in ((ax[0], "expiry", "by days to expiry"),
                         (ax[1], "delta", "by OTM delta")):  # fmt: skip
        t = markout_table(mk, by)
        for i, (b, r) in enumerate(t.iterrows()):
            lab = f"{b} ({int(r['fills'])} fills)"
            a.plot(xs, [r[f"{h} vp"] for h in labs], marker="o", markersize=4,
                   color=SERIES[i], linewidth=1.4, label=lab)  # fmt: skip
        a.axhline(0, color=AXIS, linewidth=0.8)
        a.set_xticks(xs, ["at fill", "1 min", "5 min", "1 h"])
        a.legend(frameon=False, fontsize=8, labelcolor=INK2)
        _style(a, f"Delta-hedged markout, vol points of vega, {title}")
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def fig_worst_day(rows, index: pd.Series, day: pd.Timestamp, path: Path):
    r = rows[(_t(rows["ts_ms"]) >= day) & (_t(rows["ts_ms"]) < day + pd.Timedelta("1D"))]
    g = grouped(r, "5min").cumsum() / 1e3
    s = index[(index.index >= day) & (index.index < day + pd.Timedelta("1D"))]
    fig, ax = plt.subplots(2, 1, figsize=(10, 7), sharex=True, facecolor=SURFACE,
                           gridspec_kw={"height_ratios": [2, 1]})  # fmt: skip
    for i, c in enumerate(g.columns):
        ax[0].plot(g.index, g[c], color=SERIES[i], linewidth=1.3, label=c)
    ax[0].plot(g.index, g.sum(axis=1), color=INK, linewidth=1.8, label="total")
    ax[0].axhline(0, color=AXIS, linewidth=0.8)
    ax[0].legend(frameon=False, fontsize=8, ncol=4, loc="lower left", labelcolor=INK2)
    _style(ax[0], f"Worst day {day:%d %b %Y}: cumulative P&L by component, $ thousands")
    ax[1].plot(s.index, s.to_numpy(), color=SERIES[0], linewidth=1.0)
    _style(ax[1], "ETH index, USD")
    ax[1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def build(cfg: Config, run: str):
    run_dir = Path(cfg.sim.run_dir) / run
    a, b = run.split("_")
    inp = load_inputs(cfg, pd.Timestamp(a, tz="UTC"), pd.Timestamp(b, tz="UTC"))
    f, h, led, d = (pd.read_parquet(run_dir / f"{x}.parquet")
                    for x in ("fills", "hedges", "ledger", "daily"))  # fmt: skip
    md = MarketData(inp.book, inp.curve, inp.perp)
    att = attribute(f, h, led, d, md, cfg.sim.initial_eth, inp.start_ms, inp.end_ms)
    mk = markouts(f, md, inp.end_ms, cfg.hedging.delta)
    att.rows.to_parquet(run_dir / "attribution.parquet", index=False)
    mk.to_parquet(run_dir / "markouts.parquet", index=False)
    return inp, f, h, att, mk


def write(cfg: Config, run: str) -> dict:
    inp, f, h, att, mk = build(cfg, run)
    rows, snaps = att.rows, att.snapshots
    tot = rows[COMPONENTS].sum()
    engine_end = float(snaps["engine_w"].iloc[-1]) if len(snaps) else att.w_end
    total = engine_end - att.w_start
    resid = total - float(tot.sum())
    gross = float(tot.abs().sum())
    rec = {
        "max_abs_dW": float((snaps["engine_w"] - snaps["replay_w"]).abs().max()),
        "max_abs_dB": float((snaps["engine_eth"] - snaps["replay_eth"]).abs().max()),
        "max_abs_dQ": float((snaps["engine_perp_usd"] - snaps["replay_perp_usd"]).abs().max()),
        "n_mismatch": int((snaps["engine_n"] != snaps["replay_n"]).sum()),
    }
    daily = (
        rows.assign(day=_t(rows["ts_ms"]).dt.floor("D").to_numpy()).groupby("day")[COMPONENTS].sum()
    )
    daily["total"] = daily.sum(axis=1)
    worst = daily["total"].idxmin()
    figs = Path("reports/figures")
    figs.mkdir(parents=True, exist_ok=True)
    p1, p2, p3 = (figs / f"pnl_{run}_{x}.png" for x in ("cumulative", "markouts", "worst_day"))
    idx = inp.perp.assign(t=_t(inp.perp["ts_ms"]).to_numpy()).set_index("t")["index_price_usd"]
    fig_cumulative(rows, p1)
    fig_markouts(mk, p2)
    fig_worst_day(rows, idx.resample("1min").last().dropna(), worst, p3)

    # worst day line by line
    wr = rows[(_t(rows["ts_ms"]) >= worst) & (_t(rows["ts_ms"]) < worst + pd.Timedelta("1D"))]
    hourly = grouped(wr, "1h")
    hourly["total"] = hourly.sum(axis=1)
    hourly.index = hourly.index.strftime("%H:%M")
    s_w = idx[(idx.index >= worst) & (idx.index < worst + pd.Timedelta("1D"))]
    hourly["ETH close"] = (
        s_w.resample("1h")
        .last()
        .reindex(pd.to_datetime([worst + pd.Timedelta(hours=i) for i in range(24)]))
        .to_numpy()
    )
    mkd = mk[(_t(mk["ts_ms"]) >= worst) & (_t(mk["ts_ms"]) < worst + pd.Timedelta("1D"))]
    keep = ["ts_ms", "instrument_name", "our_side", "qty", "hedged_0", "hedged_5min", "hedged_1h"]
    top = mkd.nsmallest(10, "hedged_1h")[keep].copy()
    top["ts_ms"] = _t(top["ts_ms"]).dt.strftime("%H:%M:%S").to_numpy()
    hw = h[(_t(h["exec_ms"]) >= worst) & (_t(h["exec_ms"]) < worst + pd.Timedelta("1D"))]
    hedge_cost_w = float(wr["hedge_cost"].sum())
    fill_w = wr[wr["kind"] == "fill"]

    mk_all = pd.DataFrame({lab: [mk[f"hedged_{lab}"].sum(), mk[f"raw_{lab}"].sum(),
                                 mk[f"hedged_{lab}"].notna().sum()] for lab in HORIZONS},
                          index=["delta-hedged $", "raw $", "fills"]).T  # fmt: skip
    edge = float(att.fills["edge_logged_usd"].sum()) if len(att.fills) else 0.0
    spread = float(att.fills["spread_capture"].sum()) if len(att.fills) else 0.0
    comp = tot.rename("USD").to_frame()
    comp.loc["**sum of components**"] = tot.sum()
    comp.loc["residual (engine total − sum)"] = resid
    comp.loc["**total (engine valuation)**"] = total
    dtab = daily.copy()
    dtab.index = dtab.index.strftime("%m-%d")
    md_txt = f"""# P&L attribution, run {run}

Generated by `uv run python -m mm.pnl.report --config config/base.yaml --run {run}`.
All amounts in USD. Valuation at Surface A on information up to and including each
moment (D039). Waterfall per interval, in the order time → spot → basis → vol; flows at
each fill, hedge, expiry and funding (see `src/mm/pnl/attribution.py`).

## Reconciliation

- The replayed account (from fills, hedges and the ledger) matches the engine at every
  daily snapshot: max |ΔW| ${rec["max_abs_dW"]:.4f}, max |ΔB| {rec["max_abs_dB"]:.2e} ETH,
  max |ΔQ| ${rec["max_abs_dQ"]:.2e}, position-count mismatches {rec["n_mismatch"]}.
- Start value ${att.w_start:,.0f} (the {cfg.sim.initial_eth:g} ETH collateral), end
  ${engine_end:,.0f}.
- Residual: ${resid:,.4f}, i.e. {abs(resid) / max(gross, 1) * 100:.6f}% of gross P&L
  (${gross:,.0f}).

## Components

{_md(comp.reset_index().rename(columns={"index": "component"}))}

Spread capture is measured against fair value at the fill (inclusive). The logged edge
against the quote's own fair value is ${edge:,.0f}; the difference
(${edge - spread:,.0f}) is what the quote lost to staleness (latency
{cfg.sim.latency_ms} ms, refit interval, index moves since the quote's inputs).

## By day

{_md(dtab.reset_index().rename(columns={"day": "day"}))}

![cumulative]({p1.relative_to("reports")})

## Markouts (per fill, D039)

{_md(mk_all.reset_index().rename(columns={"index": "horizon"}))}

Delta-hedged markout by expiry (USD, and vol points of the fills' vega):

{_md(markout_table(mk, "expiry").reset_index(), "{:,.2f}")}

By OTM delta:

{_md(markout_table(mk, "delta").reset_index(), "{:,.2f}")}

![markouts]({p2.relative_to("reports")})

## Worst day: {worst:%d %b %Y} (total ${daily.loc[worst, "total"]:,.0f})

{len(fill_w):,} fills (spread capture ${fill_w["spread_capture"].sum():,.0f}), {len(hw):,}
hedges (hedge cost ${hedge_cost_w:,.0f}, perp fees ${wr["perp_fee"].sum():,.0f}).

By hour (UTC):

{_md(hourly.reset_index().rename(columns={"t": "hour", "index": "hour"}))}

The 10 fills with the worst 1-hour delta-hedged markout:

{_md(top, "{:,.0f}")}

![worst day]({p3.relative_to("reports")})
"""
    out = Path(f"reports/pnl_{run}.md")
    out.write_text(md_txt)
    summary = {"run": run, "total": total, "residual": resid, "gross": gross, **rec,
               "components": {k: float(v) for k, v in tot.items()},
               "worst_day": str(worst.date()), "worst_day_total": float(daily.loc[worst, "total"]),
               "markouts": {lab: float(mk[f"hedged_{lab}"].sum()) for lab in HORIZONS}}  # fmt: skip
    (Path(cfg.sim.run_dir) / run / "pnl_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(prog="mm.pnl.report", description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--run", required=True)
    a = p.parse_args(argv)
    s = write(load_config(a.config), a.run)
    print(f"total ${s['total']:,.0f}, residual ${s['residual']:,.4f}, worst day {s['worst_day']} "
          f"(${s['worst_day_total']:,.0f}) -> reports/pnl_{a.run}.md")  # fmt: skip


if __name__ == "__main__":
    main()
