"""Run figures: account delta vs the hedge band, and book vega vs the vega limit."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from mm.config import Config  # noqa: E402
from mm.sim.engine import SimInputs, SimResult  # noqa: E402

SURFACE, INK, INK2, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]


def _style(ax, title):
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=10, color=INK, pad=6)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=INK2, labelsize=8, length=0)


def _t(ms) -> pd.DatetimeIndex:
    return pd.to_datetime(pd.Series(ms), unit="ms", utc=True)


def delta_figure(cfg: Config, inp: SimInputs, res: SimResult, path: Path) -> None:
    band = cfg.hedging.delta_band_eth
    d = res.delta.assign(t=_t(res.delta["ts_ms"])).set_index("t")["delta_eth"]
    # per minute: the largest |delta| seen (keeps sign), so breaches stay visible
    m = d.groupby(d.index.floor("1min")).agg(lambda x: x.iloc[x.abs().argmax()])
    s = inp.perp.assign(t=_t(inp.perp["ts_ms"])).set_index("t")["index_price_usd"]
    s = s.resample("1min").last().dropna()
    fig, ax = plt.subplots(2, 1, figsize=(10, 6), sharex=True, facecolor=SURFACE,
                           gridspec_kw={"height_ratios": [2, 1]})  # fmt: skip
    ax[0].axhspan(-band, band, color=GRID, alpha=0.6, linewidth=0, label=f"band ±{band:g} ETH")
    ax[0].plot(m.index, m.to_numpy(), color=SERIES[0], linewidth=0.8,
               label="account delta (largest |Δ| per minute)")  # fmt: skip
    ax[0].set_ylim(-3 * band, 3 * band)
    n_out = int((m.abs() > 3 * band).sum())
    _style(ax[0], f"Account delta, ETH (clipped at ±{3 * band:g}; {n_out} minutes beyond)")
    ax[0].legend(frameon=False, fontsize=8, loc="upper left", labelcolor=INK2)
    ax[1].plot(s.index, s.to_numpy(), color=SERIES[0], linewidth=1.0)
    _style(ax[1], "ETH index, USD")
    ax[1].xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def vega_figure(cfg: Config, res: SimResult, path: Path) -> None:
    lim = cfg.risk.vega_limit_usd
    q = res.quotes.assign(t=_t(res.quotes["ts_ms"])).set_index("t")
    if q.empty:
        return
    cut = (q["bid_size"] < q["size"]) | (q["ask_size"] < q["size"])
    one = (q["bid_size"] == 0) | (q["ask_size"] == 0)
    h = pd.DataFrame({"cut": cut, "one": one}).resample("1h").mean() * 100
    fig, ax = plt.subplots(2, 1, figsize=(10, 6), sharex=True, facecolor=SURFACE)
    for y in (lim, -lim):
        ax[0].axhline(y, color=AXIS, linewidth=1.0, linestyle="--")
    ax[0].plot(q.index, q["net_vega_usd"], color=SERIES[0], linewidth=0.8)
    ax[0].text(q.index[0], lim, f" vega limit ±${lim:,.0f}", color=INK2, fontsize=8, va="bottom")
    _style(ax[0], "Net book vega at each quote, USD per vol point")
    ax[1].plot(h.index, h["cut"], color=SERIES[0], linewidth=1.0, label="a side cut or dropped")
    ax[1].plot(
        h.index, h["one"], color=SERIES[1], linewidth=1.0, label="one-sided (a side dropped)"
    )
    ax[1].set_ylim(0, 100)
    _style(ax[1], "Quotes cut by a limit, % per hour")
    ax[1].legend(frameon=False, fontsize=8, loc="upper left", labelcolor=INK2)
    ax[1].xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
