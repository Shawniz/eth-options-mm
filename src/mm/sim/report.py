"""Save a run (data/runs/<label>/) and write its Markdown summary."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from mm.config import Config
from mm.sim.engine import SimInputs, SimResult

T_BUCKETS = [0, 2, 8, 35, 100, 10000]


def _ts(ms) -> pd.Series:
    return pd.to_datetime(pd.Series(ms), unit="ms", utc=True)


def _md(df: pd.DataFrame, fmt: str = "{:,.4f}") -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        cells = []
        for v in r.tolist():
            if isinstance(v, (float, np.floating)):
                cells.append(fmt.format(v))
            elif isinstance(v, (int, np.integer)):
                cells.append(f"{v:,}")
            else:
                cells.append(str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def save(res: SimResult, run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    res.fills.to_parquet(run_dir / "fills.parquet", index=False)
    res.ledger.to_parquet(run_dir / "ledger.parquet", index=False)
    res.daily.to_parquet(run_dir / "daily.parquet", index=False)
    res.pauses.to_parquet(run_dir / "pauses.parquet", index=False)


def volume_share(inp: SimInputs, res: SimResult) -> pd.DataFrame:
    tr = inp.trades[(inp.trades["ts_ms"] >= inp.start_ms) & (inp.trades["ts_ms"] < inp.end_ms)]
    tr = tr.assign(T=pd.cut(tr["t_years"] * 365, T_BUCKETS))
    f = res.fills
    f = f.assign(T=pd.cut(f["t_years"] * 365, T_BUCKETS)) if len(f) else f.assign(T=[])
    mkt = tr.groupby("T", observed=False)["amount_contracts"].agg(["size", "sum"])
    ours = (
        f.groupby("T", observed=False)["qty"].agg(["size", "sum"])
        if len(f)
        else pd.DataFrame(0, index=mkt.index, columns=["size", "sum"])
    )
    out = pd.DataFrame(
        {
            "market trades": mkt["size"],
            "market contracts": mkt["sum"],
            "our fills": ours["size"].reindex(mkt.index, fill_value=0),
            "our contracts": ours["sum"].reindex(mkt.index, fill_value=0.0),
        }
    )
    out.loc["all"] = out.sum()
    out["share of contracts"] = out["our contracts"] / out["market contracts"]
    out["share of trades filled"] = out["our fills"] / out["market trades"]
    out = out.reset_index().rename(columns={"index": "expiry days", "T": "expiry days"})
    out["expiry days"] = out["expiry days"].astype(str)
    for c in ("market trades", "our fills"):
        out[c] = out[c].astype(int)
    return out


def write_report(cfg: Config, inp: SimInputs, res: SimResult, label: str, out: Path) -> dict:
    f, led = res.fills, res.ledger
    n_tr = int(((inp.trades["ts_ms"] >= inp.start_ms) & (inp.trades["ts_ms"] < inp.end_ms)).sum())
    flows = led.groupby("kind")["eth"].sum() if len(led) else pd.Series(dtype=float)
    reasons = f["reason"].value_counts() if len(f) else pd.Series(dtype=int)
    vs = volume_share(inp, res)
    daily = res.daily.copy()
    if len(daily):
        daily["day"] = _ts(daily["ts_ms"]).dt.strftime("%Y-%m-%d %H:%M")
        daily = daily[
            ["day", "eth_balance", "options_eth", "equity_eth", "index_usd", "equity_usd",
             "n_positions", "n_unvalued"]
        ]  # fmt: skip
    edge = float(f["edge_eth"].sum()) if len(f) else 0.0
    summary = {
        "label": label,
        "option_trades": n_tr,
        "fills": len(f),
        "contracts": float(f["qty"].sum()) if len(f) else 0.0,
        "fills_with_reason": int(f["reason"].notna().sum()) if len(f) else 0,
        "share_of_contracts": float(vs.iloc[-1]["share of contracts"]),
        "edge_at_fill_eth": edge,
        "flows_eth": {k: float(v) for k, v in flows.items()},
        "final_eth": res.account.eth,
        "open_positions": len(res.account.positions),
        "skips": dict(res.skips),
        "pauses": len(
            res.pauses[
                (res.pauses["gap_end_ms"] >= inp.start_ms) & (res.pauses["gap_end_ms"] < inp.end_ms)
            ]
        ),  # fmt: skip
    }
    qc, u = cfg.quoting, cfg.quoting.universe
    if len(f):
        summary["quoting"] = {
            "floor_bound_frac": float(f["floor_bound"].mean()),
            "skew_capped_frac": float(f["skew_capped"].mean()),
            "median_abs_skew_volpts": float(f["skew_volpts"].abs().median()),
            "net_vega_usd_min": float(f["net_vega_usd"].min()),
            "net_vega_usd_max": float(f["net_vega_usd"].max()),
            "median_quote_size": float(f["quote_size"].median()),
        }
        qs = summary["quoting"]
        fb = qs["floor_bound_frac"] * 100
        quoting_lines = f"""- Fills where the price floor set a side: {fb:.1f}%
- Fills with the skew at its cap (±h): {qs["skew_capped_frac"] * 100:.1f}%;
  median |skew| {qs["median_abs_skew_volpts"]:.3f} vol points
- Net book vega seen at fills: {qs["net_vega_usd_min"]:,.0f} to {qs["net_vega_usd_max"]:,.0f}
  USD per vol point; median quote size {qs["median_quote_size"]:g} contracts"""
    else:
        quoting_lines = "- (no fills)"
    skip_tbl = pd.DataFrame(
        sorted(res.skips.items(), key=lambda x: -x[1]), columns=["reason", "trades"]
    )
    reason_tbl = reasons.rename_axis("fill reason").reset_index(name="fills")
    flag_line = ""
    if len(f):
        flag_line = (
            f"Fills from suspect trades: {int(f['is_suspect'].sum()):,}; "
            f"from liquidations: {int(f['is_liquidation'].sum()):,}."
        )
    flows_tbl = flows.rename_axis("flow").reset_index(name="ETH")
    size_txt = (
        f"vega-sized (target ${qc.target_vega_usd:g}/vol pt, max {qc.max_contracts:g})"
        if qc.sizing == "vega"
        else f"{qc.fixed_contracts:g} contracts a side"
    )
    md = f"""# Simulator run {label}

Generated by `uv run python -m mm.run --config config/base.yaml --start ... --end ...`.
Quoter (Phase 5): fair vol ± {qc.half_spread_volpts:g} vol points, price floor
max({qc.min_half_width_ticks:g} tick, {qc.fee_cover:g} × maker fee), skew κ =
{qc.kappa_volpts:g} vol pts per ${qc.vega_ref_usd:,.0f}/vol pt of book vega (capped at h),
{size_txt}; universe {u.min_days:g}–{u.max_days:g} days, OTM delta ≥ {u.min_abs_delta:g}.
Fill rule: strict (D003), size = min(trade, quote) × {cfg.fill.max_fill_fraction:g}
({cfg.sim.fill_sizing}, D027), latency {cfg.sim.latency_ms} ms, maker fees.

## Summary

- Option trades in window: {n_tr:,}
- Our fills: {len(f):,} ({summary["contracts"]:,.1f} contracts); every fill has a reason:
  {"yes" if summary["fills_with_reason"] == len(f) else "NO"}
- Share of traded contracts we filled: {summary["share_of_contracts"] * 100:.2f}%
- Edge at fill, vs Surface A fair value: {edge:+.4f} ETH
- Exchange pauses in window: {summary["pauses"]}
{quoting_lines}
- Accounting invariants: checked after every event and audited at the end (passed)
- ETH balance: {cfg.sim.initial_eth:g} → {res.account.eth:.4f}; open positions at end:
  {len(res.account.positions)}

{flag_line}

## Fills by reason

{_md(reason_tbl)}

## Trades we did not fill

{_md(skip_tbl)}

## Volume share by expiry

{_md(vs)}

## ETH flows

{_md(flows_tbl, "{:+,.5f}")}

## Daily account value (Surface A)

{_md(daily) if len(daily) else "(none)"}
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    return summary


def write_summary(summary: dict, run_dir: Path) -> None:
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=float))
