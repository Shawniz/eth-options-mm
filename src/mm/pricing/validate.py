"""Phase 2 validation report: do we reproduce Deribit's own trade IVs?

    uv run python -m mm.pricing.validate --config config/base.yaml

Reads the priced cache (mm.pricing.enrich) and writes reports/phase2_validation.md.
Exit code 1 if the done-when check fails.

Pass/fail (decision D018) uses the INCLUSIVE view: our forward at the trade instant
(basis up to t, the trade's own index), i.e. the inputs Deribit itself had. The STRICT
view (< t - latency) is reported as "information lag": what a resting quote could know.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import brentq

from mm.config import Config, load_config
from mm.pricing.black76 import delta, price_eth
from mm.pricing.enrich import CACHE_NAME, build_curve
from mm.pricing.iv import Status

T_BUCKETS = [1, 2, 4, 8, 30, 100, 1000]
DELTA_BUCKETS = [0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0]


def load_priced(cfg: Config) -> pd.DataFrame:
    import duckdb

    glob = (Path("data/cache") / CACHE_NAME / "date=*" / "*.parquet").as_posix()
    return duckdb.sql(f"select * from read_parquet('{glob}', hive_partitioning=true)").df()


def _summary(err_volpts: pd.Series) -> dict:
    a = err_volpts.dropna().to_numpy()
    if len(a) == 0:
        return {"n": 0, "median": np.nan, "p95": np.nan, "p99": np.nan}
    return {
        "n": len(a),
        "median": np.median(a),
        "p95": np.percentile(a, 95),
        "p99": np.percentile(a, 99),
    }


def _table(df: pd.DataFrame, col: str, by: str) -> pd.DataFrame:
    rows = []
    for key, g in df.groupby(by, observed=True):
        rows.append({by: str(key), **_summary(g[col])})
    rows.append({by: "all", **_summary(df[col])})
    return pd.DataFrame(rows)


def _md(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        cells = [
            f"{v:,.0f}" if c == "n" else (f"{v:.3f}" if isinstance(v, float) else str(v))
            for c, v in zip(cols, r.tolist(), strict=True)
        ]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def sample_universe(p: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    v = cfg.pricing.validation
    keep = (
        (p["trade_iv_status"] == Status.OK)
        & ~p["is_suspect"]
        & (p["t_years"] * 365 > v.min_days_to_expiry)
        & (p["iv"] > 0)
    )
    s = p[keep].copy()
    s["err_incl"] = (s["trade_iv_ours"] - s["iv"]).abs() * 100
    s["err_strict"] = (s["trade_iv_strict"] - s["iv"]).abs() * 100
    call = (s["option_type"] == "C").to_numpy()
    d = delta(
        s["forward_usd"].to_numpy(),
        s["strike_usd"].to_numpy(),
        s["t_years"].to_numpy(),
        s["trade_iv_ours"].to_numpy(),
        call,
    )
    s["T_days"] = pd.cut(s["t_years"] * 365, T_BUCKETS)
    s["abs_delta"] = pd.cut(np.abs(d), DELTA_BUCKETS)
    s["month"] = s["ts"].dt.strftime("%Y-%m")
    return s


def deribit_implied_forward_gap(s: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """DIAGNOSTIC ONLY (never used by the pipeline): the forward Deribit must have used,
    backed out of its trade IV, compared with ours and with the parity alternative."""
    v = cfg.pricing.validation
    smp = s.sample(min(v.forward_gap_sample, len(s)), random_state=v.seed).copy()

    def solve(r):
        def g(f):
            return price_eth(f, r.strike_usd, r.t_years, r.iv, r.option_type == "C") - r.price_eth

        try:
            return brentq(g, r.index_price_usd * 0.9, r.index_price_usd * 1.3, xtol=1e-6)
        except ValueError:
            return np.nan

    smp["F_deribit"] = smp.apply(solve, axis=1)
    pcfg = cfg.model_copy(
        update={
            "pricing": cfg.pricing.model_copy(
                update={
                    "forwards": cfg.pricing.forwards.model_copy(
                        update={"non_future_source": "parity"}
                    )
                }
            )
        }
    )
    parity_curve = build_curve(pcfg)
    pf = parity_curve.forwards(
        smp["ts"], smp["expiry"], 0, inclusive=True, index=smp["index_price_usd"]
    )
    smp["F_parity"] = pf["forward_usd"].to_numpy()
    smp["parity_src"] = pf["source"].to_numpy()
    # Identifiable rows only: where the price is sensitive to F (|delta - p| > 0.05)
    call = (smp["option_type"] == "C").to_numpy()
    sens = np.abs(
        delta(smp["forward_usd"], smp["strike_usd"], smp["t_years"], smp["iv"], call)
        - smp["price_eth"]
    )
    smp = smp[(sens > 0.05) & smp["F_deribit"].notna()]
    rows = []
    for src, g in smp.groupby("fwd_source", observed=True):
        gap = (g["forward_usd"] / g["F_deribit"] - 1) * 1e4
        row = {
            "our source": src,
            "n": len(g),
            "median gap bp": gap.median(),
            "median abs gap bp": gap.abs().median(),
        }
        pg = g[g["parity_src"] == "parity"]
        if len(pg) and src != "future":
            pgap = (pg["F_parity"] / pg["F_deribit"] - 1) * 1e4
            row |= {
                "parity: n": len(pg),
                "parity: median gap bp": pgap.median(),
                "parity: median abs gap bp": pgap.abs().median(),
            }
        rows.append(row)
    return pd.DataFrame(rows)


def write_report(cfg: Config, out: Path) -> bool:
    v = cfg.pricing.validation
    p = load_priced(cfg)
    s = sample_universe(p, cfg)
    head = _summary(s["err_incl"])
    passed = head["median"] <= v.target_median_volpts and head["p95"] <= v.target_p95_volpts
    strict = _summary(s["err_strict"])
    status_counts = (
        p["mark_iv_status"]
        .map(lambda x: Status(int(x)).name)
        .value_counts()
        .rename_axis("mark_iv status")
        .reset_index(name="n")
    )
    fgap = deribit_implied_forward_gap(s, cfg)
    src_counts = p["fwd_source"].value_counts().rename_axis("forward source").reset_index(name="n")

    target = f"median ≤ {v.target_median_volpts}, p95 ≤ {v.target_p95_volpts}"

    def _row(label, x, tgt):
        return f"| {label} | {x['median']:.3f} | {x['p95']:.3f} | {x['p99']:.3f} | {tgt} |"

    row_incl = _row("inclusive (inputs at the trade instant), **pass/fail**", head, target)
    row_strict = _row(
        f"strict (< t − {cfg.sim.latency_ms} ms), information lag", strict, "reported only"
    )
    md = f"""# Phase 2 validation: reproducing Deribit's trade IVs

Generated by `uv run python -m mm.pricing.validate --config config/base.yaml`.

**Test:** for each historical trade, our IV solved from the trade price with our forward,
compared with Deribit's own `iv` on that trade (computed by Deribit with its forward at
the trade instant). Non-circular: Deribit's IV never feeds our forward.

**Universe:** {head["n"]:,} trades (of {len(p):,}): solver OK, not flagged suspect,
more than {v.min_days_to_expiry:g} day to expiry, Deribit iv > 0.

## Result: {"PASS" if passed else "FAIL"}

| view | median | p95 | p99 | target |
|---|---|---|---|---|
{row_incl}
{row_strict}

All errors in vol points (1 = 0.01 vol).

## Inclusive view by expiry (days)

{_md(_table(s, "err_incl", "T_days"))}

## Inclusive view by |delta|

{_md(_table(s, "err_incl", "abs_delta"))}

## Inclusive view by forward source

{_md(_table(s, "err_incl", "fwd_source"))}

## Inclusive view by month

{_md(_table(s, "err_incl", "month"))}

## Information lag (strict view) by expiry and |delta|

What a quote resting before each trade could have known at {cfg.sim.latency_ms} ms latency.
Short-dated and deep in-the-money options amplify small index moves into large vol errors:
an early view of their adverse-selection risk.

{_md(_table(s, "err_strict", "T_days"))}

{_md(_table(s, "err_strict", "abs_delta"))}

## Forward diagnostic: gap to the forward Deribit must have used (bp)

Diagnostic only. Backed out of Deribit's trade IV on a random sample of
{v.forward_gap_sample:,} trades (seed {v.seed}), rows where the price is sensitive to F.
Compares our forward with the put-call-parity alternative (decision D017).

{_md(fgap)}

## Coverage

{_md(src_counts)}

{_md(status_counts)}
"""
    out.write_text(md)
    return passed


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="mm.pricing.validate", description=__doc__)
    p.add_argument("--config", required=True)
    a = p.parse_args(argv)
    ok = write_report(load_config(a.config), Path("reports/phase2_validation.md"))
    print("PASS" if ok else "FAIL", "-> reports/phase2_validation.md")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
