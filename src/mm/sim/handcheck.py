"""Hand-check fills: recompute each one from raw tables, without the simulator's code.

    uv run python -m mm.sim.handcheck --config config/base.yaml --run 2025-01-06_2025-01-13

For N random fills (seeded) it recomputes, with its own queries and formulas:
  refit used     latest refit <= t - latency (refits.parquet)
  index          latest index on any stream strictly before t - latency (DuckDB)
  forward        for expiries with a dated future: latest future mark/index before the
                 cutoff x index. Expiries without one (interpolated basis) use the run's
                 forward, which Phase 2 validated; the table says which.
  fair vol       raw SVI w(k) of that refit's slice: sqrt(w / t_ref)
  quotes         Black-76 with scipy.stats.norm, +- half-spread, own tick rounding
  fill           strict rule, size min(trade, quote) x fraction, maker fee with cap
Writes reports/phase4_handcheck.md.
"""

from __future__ import annotations

import argparse
import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

from mm.config import load_config
from mm.data.db import connect
from mm.surface.build import CACHE

MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
MONTHS = {m: i + 1 for i, m in enumerate(MON)}


def _expiry(name: str) -> datetime:
    code = name.split("-")[1]  # e.g. 10JAN25
    return datetime(2000 + int(code[-2:]), MONTHS[code[-5:-2]], int(code[:-5]), 8, tzinfo=UTC)


def _black_eth(f, k, t, s, call):
    sd = s * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    usd = f * norm.cdf(d1) - k * norm.cdf(d2) if call else k * norm.cdf(-d2) - f * norm.cdf(-d1)
    return usd / f


def _tick(p, steps):
    t = steps[0].tick_eth
    for s in steps:
        if p > s.above_price_eth:
            t = s.tick_eth
    return t


def check(cfg, run: str, n: int, seed: int) -> pd.DataFrame:
    fills = pd.read_parquet(Path(cfg.sim.run_dir) / run / "fills.parquet")
    pick = fills.sample(n=min(n, len(fills)), random_state=seed).sort_values("ts_ms")
    refit_grid = np.sort(pd.read_parquet(CACHE / "refits.parquet")["refit_ts"]
                         .astype("datetime64[ms, UTC]").astype("int64").to_numpy())  # fmt: skip
    fits = pd.read_parquet(CACHE / "fits.parquet")
    con = connect(cfg.data.clean_dir)
    spec, qc, fc = cfg.specs.option, cfg.quoting.fixed, cfg.fill
    lat = cfg.sim.latency_ms
    rows = []
    for r in pick.itertuples():
        ts = pd.Timestamp(r.ts_ms, unit="ms", tz="UTC")
        cut = r.ts_ms - lat
        cut_s = pd.Timestamp(cut, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M:%S.%f")
        exp = _expiry(r.instrument_name)
        exp_ms = int(exp.timestamp() * 1000)
        refit = int(refit_grid[np.searchsorted(refit_grid, cut, side="right") - 1])
        idx = con.execute(
            f"""select index_price_usd from (
              select ts, index_price_usd from perp_trades where ts < timestamp '{cut_s}'
                and ts > timestamp '{cut_s}' - interval 1 hour
              union all select ts, index_price_usd from future_trades where ts < timestamp
                '{cut_s}' and ts > timestamp '{cut_s}' - interval 1 hour
              union all select ts, index_price_usd from option_trades where ts < timestamp
                '{cut_s}' and ts > timestamp '{cut_s}' - interval 1 hour)
            order by ts desc limit 1"""
        ).fetchone()[0]
        max_age = int(cfg.pricing.forwards.future_basis_max_age_s)
        fut = con.execute(
            f"""select mark_price_usd / index_price_usd from future_trades
            where expiry = timestamp '{exp:%Y-%m-%d %H:%M:%S}' and ts < timestamp '{cut_s}'
              and ts > timestamp '{cut_s}' - interval {max_age} second
            order by ts desc, trade_seq desc limit 1"""
        ).fetchone()  # fmt: skip
        if fut is not None:
            fwd, src = fut[0] * idx, "future"
        else:
            fwd, src = r.forward_q, "interp (run value)"
        sl = fits[(fits["refit_ts"] == pd.Timestamp(refit, unit="ms", tz="UTC"))
                  & (fits["expiry"] == pd.Timestamp(exp))].iloc[0]  # fmt: skip
        k = math.log(r.strike_usd / fwd)
        w = sl.a + sl.b * (sl.rho * (k - sl.m) + math.sqrt((k - sl.m) ** 2 + sl.s**2))
        sig = math.sqrt(w / sl.t_ref)
        t = (exp_ms - r.ts_ms) / (365 * 86_400_000)
        call = r.instrument_name.endswith("-C")
        h = qc.half_spread_volpts / 100
        pb, pa = (
            _black_eth(fwd, r.strike_usd, t, sig - h, call),
            _black_eth(fwd, r.strike_usd, t, sig + h, call),
        )
        tb, ta = _tick(pb, spec.tick_steps), _tick(pa, spec.tick_steps)
        bid = round(math.floor(pb / tb + 1e-9) * tb, 10)
        ask = round(math.ceil(pa / ta - 1e-9) * ta, 10)
        if r.direction == "sell":
            crosses, our_px = bid > r.price_eth + 1e-12, bid
        else:
            crosses, our_px = ask < r.price_eth - 1e-12, ask
        qty = min(r.amount_contracts, qc.size_contracts) * fc.max_fill_fraction
        fee = qty * min(spec.maker_fee_eth_per_contract, spec.fee_cap_fraction_of_premium * our_px)
        sweep = abs(r.eligible - min(r.amount_contracts, qc.size_contracts)) > 1e-9
        ok = (
            refit == r.refit_ms
            and abs(idx - r.index_q) < 1e-9
            and abs(fwd - r.forward_q) < 1e-6 * fwd
            and crosses
            and abs(our_px - r.fill_price) < 1e-12
            and (sweep or abs(qty - r.qty) < 1e-12)
            and (sweep or abs(fee - r.fee_eth) < 1e-12)
        )
        rows.append(
            {
                "time (UTC)": ts.strftime("%m-%d %H:%M:%S.%f")[:-3],
                "instrument": r.instrument_name,
                "taker": r.direction,
                "trade px": r.price_eth,
                "size": r.amount_contracts,
                "fwd source": src,
                "fair vol": sig,
                "bid (check)": bid,
                "ask (check)": ask,
                "bid (sim)": r.bid,
                "ask (sim)": r.ask,
                "our side": r.our_side,
                "fill px": r.fill_price,
                "qty (check/sim)": f"{qty:g}/{r.qty:g}" + (" sweep" if sweep else ""),
                "fee (check)": fee,
                "fee (sim)": r.fee_eth,
                "ok": "✅" if ok else "❌",
            }
        )
    return pd.DataFrame(rows)


def main(argv=None):
    p = argparse.ArgumentParser(prog="mm.sim.handcheck", description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    df = check(cfg, a.run, a.n, a.seed)
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        cells = [f"{v:.5f}" if isinstance(v, float) else str(v) for v in r.tolist()]
        lines.append("| " + " | ".join(cells) + " |")
    n_ok = int((df["ok"] == "✅").sum())
    md = f"""# Phase 4: hand-checked fills ({a.run})

Generated by `uv run python -m mm.sim.handcheck --config {a.config} --run {a.run}`.
{len(df)} fills sampled at random (seed {a.seed}) and recomputed from the raw tables with
independent queries and formulas (see the module docstring). **{n_ok}/{len(df)} match.**
Prices in ETH; fair vol decimal. "sweep": the trade was a later leg of one taker order,
so less of our quote was left (size checked against the sweep rule instead).

{chr(10).join(lines)}
"""
    Path("reports/phase4_handcheck.md").write_text(md)
    print(f"{n_ok}/{len(df)} fills match -> reports/phase4_handcheck.md")


if __name__ == "__main__":
    main()
