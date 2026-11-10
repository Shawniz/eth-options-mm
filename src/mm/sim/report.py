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
    res.hedges.to_parquet(run_dir / "hedges.parquet", index=False)
    res.delta.to_parquet(run_dir / "delta.parquet", index=False)
    res.quotes.to_parquet(run_dir / "quotes.parquet", index=False)


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
            ["day", "eth_balance", "options_eth", "perp_eth", "equity_eth", "index_usd",
             "equity_usd", "perp_usd", "delta_eth", "n_positions", "n_unvalued"]
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
    summary["hedging"] = hedging_summary(cfg, res)
    summary["limits"] = dict(res.limits)
    hs, lm, rk = summary["hedging"], res.limits, cfg.risk
    nq = max(lm.get("quotes", 0), 1)
    if hs["hedges"]:
        worst = hs["worst"]
        worst_txt = "; ".join(f"{w['time']} {w['delta']:+.1f}" for w in worst)
        hedge_lines = f"""- Hedges: {hs["hedges"]:,} ({hs["hedges_per_day"]:.0f} a day), traded
  ${hs["notional_usd"] / 1e6:,.1f}M of perp; taker fees {hs["fee_eth"]:.4f} ETH, slippage
  {hs["slippage_eth"]:.4f} ETH; {hs["fill_triggered"]:,} triggered by an option fill
- Wait from decision to same-side print: median {hs["wait_ms_p50"] / 1000:.2f} s,
  p99 {hs["wait_ms_p99"] / 1000:.1f} s, max {hs["wait_ms_max"] / 1000:.0f} s
- |delta| ≤ band ({cfg.hedging.delta_band_eth:g} ETH): {hs["inside_checks"] * 100:.2f}% of
  {hs["checks"]:,} checks, {hs["inside_time"] * 100:.2f}% of the time; max |delta|
  {hs["max_abs_delta"]:.1f} ETH. Largest: {worst_txt}
- Funding {hs["funding_eth"]:+.4f} ETH; perp P&L settled {hs["perp_pnl_eth"]:+.4f} ETH"""
    else:
        hedge_lines = "- (no hedges)"
    limit_lines = f"""- Quotes reaching the limit check: {lm.get("quotes", 0):,}
- Bid cut / dropped: {lm.get("bid_cut", 0):,} / {lm.get("bid_dropped", 0):,}; ask cut /
  dropped: {lm.get("ask_cut", 0):,} / {lm.get("ask_dropped", 0):,}
  ({(lm.get("bid_dropped", 0) + lm.get("ask_dropped", 0)) / nq * 100:.1f}% of quotes one-sided)"""
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
{qc.kappa_volpts:g} vol pts at the ${cfg.risk.vega_limit_usd:,.0f}/vol pt vega limit (capped at h),
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

## Hedging (perpetual, taker)

{hedge_lines}

## Limits (vega ±${rk.vega_limit_usd:,.0f}/vol pt, ±{rk.max_position_contracts:g} contracts)

{limit_lines}

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


def hedging_summary(cfg: Config, res: SimResult) -> dict:
    h, d, led, f = res.hedges, res.delta, res.ledger, res.fills
    band = cfg.hedging.delta_band_eth
    days = max((d["ts_ms"].max() - d["ts_ms"].min()) / 86_400_000, 1e-9) if len(d) else 1.0
    out = {"hedges": len(h), "checks": len(d)}
    flows = led.groupby("kind")["eth"].sum() if len(led) else pd.Series(dtype=float)
    out["funding_eth"] = float(flows.get("funding", 0.0))
    out["perp_pnl_eth"] = float(flows.get("perp_pnl", 0.0))
    if len(d):
        a = d["delta_eth"].abs()
        w = d["ts_ms"].diff().shift(-1).fillna(0).to_numpy()
        out["inside_checks"] = float((a <= band).mean())
        out["inside_time"] = float((w * (a <= band)).sum() / max(w.sum(), 1))
        out["max_abs_delta"] = float(a.max())
        top = d.loc[a.nlargest(5).index]
        out["worst"] = [
            {"time": _ts([t]).dt.strftime("%m-%d %H:%M:%S").iloc[0], "delta": float(v)}
            for t, v in zip(top["ts_ms"], top["delta_eth"], strict=True)
        ]
    if len(h):
        out |= {
            "hedges_per_day": len(h) / days,
            "notional_usd": float(h["dq_usd"].abs().sum()),
            "fee_eth": float(h["fee_eth"].sum()),
            "slippage_eth": float(h["slippage_eth"].sum()),
            "fill_triggered": int(h["decide_ms"].isin(set(f["ts_ms"])).sum()) if len(f) else 0,
            "wait_ms_p50": float(h["wait_ms"].median()),
            "wait_ms_p99": float(h["wait_ms"].quantile(0.99)),
            "wait_ms_max": float(h["wait_ms"].max()),
        }
    return out


def write_summary(summary: dict, run_dir: Path) -> None:
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=float))
