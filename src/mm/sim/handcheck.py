"""Hand-check fills: recompute each one from raw tables, without the simulator's code.

    uv run python -m mm.sim.handcheck --config config/base.yaml --run 2025-01-06_2025-01-13

For N random fills (seeded) it recomputes, with its own queries and formulas:
  refit used     latest refit <= t - latency (refits.parquet)
  index          latest index on any stream strictly before t - latency (DuckDB)
  forward        for expiries with a dated future: latest future mark/index before the
                 cutoff x index. Expiries without one (interpolated basis) use the run's
                 forward, which Phase 2 validated; the table says which.
  fair vol       raw SVI w(k) of that refit's slice: sqrt(w / t_ref)
  universe       tenor and OTM delta min(N(d1), 1 - N(d1)) at fair vol
  quotes         Black-76 with scipy.stats.norm: skew -kappa x net_vega / vega_limit capped
                 at h, +- h around the centre, price floor max(ticks, fee), own rounding.
                 net_vega is taken from the fill log (it depends on the whole book's
                 history); everything downstream of it is recomputed.
  size           vega: floor(min(target / (Black vega x S/F), max)); fixed: fixed_contracts;
                 then our side cut to the room under the vega and position limits, from the
                 logged net vega and position before the fill
  fill           strict rule, size min(trade, quote) x fraction, maker fee with cap
Writes reports/phase5_handcheck.md (--out).
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
    spec, qc, fc = cfg.specs.option, cfg.quoting, cfg.fill
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
        sd = sig * math.sqrt(t)
        d1 = (math.log(fwd / r.strike_usd) + 0.5 * sd * sd) / sd
        otm = min(norm.cdf(d1), 1 - norm.cdf(d1))
        u = qc.universe
        in_univ = u.min_days <= t * 365 <= u.max_days and otm >= u.min_abs_delta
        vega_i = fwd * norm.pdf(d1) * math.sqrt(t) / 100 * idx / fwd
        raw_size = (qc.fixed_contracts if qc.sizing == "fixed"
                    else min(qc.target_vega_usd / vega_i, qc.max_contracts))  # fmt: skip
        inc = spec.min_order_contracts
        base = math.floor(raw_size / inc + 1e-9) * inc
        rk = cfg.risk
        if r.our_side == "buy":
            room = min(rk.max_position_contracts - r.pos_before,
                       (rk.vega_limit_usd - r.net_vega_usd) / vega_i)  # fmt: skip
        else:
            room = min(rk.max_position_contracts + r.pos_before,
                       (rk.vega_limit_usd + r.net_vega_usd) / vega_i)  # fmt: skip
        size = math.floor(min(base, room) / inc + 1e-9) * inc
        side_size = r.bid_size if r.our_side == "buy" else r.ask_size
        h = qc.half_spread_volpts / 100
        skew = -qc.kappa_volpts * r.net_vega_usd / cfg.risk.vega_limit_usd / 100
        skew = max(-h, min(h, skew))
        sc = sig + skew
        pc = _black_eth(fwd, r.strike_usd, t, sc, call)
        fee_c = min(spec.maker_fee_eth_per_contract, spec.fee_cap_fraction_of_premium * pc)
        half_min = max(qc.min_half_width_ticks * _tick(pc, spec.tick_steps), qc.fee_cover * fee_c)
        pb = min(_black_eth(fwd, r.strike_usd, t, sc - h, call), pc - half_min)
        pa = max(_black_eth(fwd, r.strike_usd, t, sc + h, call), pc + half_min)
        tb, ta = _tick(pb, spec.tick_steps), _tick(pa, spec.tick_steps)
        bid = round(math.floor(pb / tb + 1e-9) * tb, 10)
        ask = round(math.ceil(pa / ta - 1e-9) * ta, 10)
        if ask - bid < _tick(bid, spec.tick_steps) - 1e-9:
            ask = round(bid + _tick(bid, spec.tick_steps), 10)
        if r.direction == "sell":
            crosses, our_px = bid > r.price_eth + 1e-12, bid
        else:
            crosses, our_px = ask < r.price_eth - 1e-12, ask
        qty = min(r.amount_contracts, size) * fc.max_fill_fraction
        fee = qty * min(spec.maker_fee_eth_per_contract, spec.fee_cap_fraction_of_premium * our_px)
        sweep = abs(r.eligible - min(r.amount_contracts, size)) > 1e-9
        ok = (
            refit == r.refit_ms
            and abs(idx - r.index_q) < 1e-9
            and abs(fwd - r.forward_q) < 1e-6 * fwd
            and in_univ
            and abs(base - r.quote_size) < 1e-9
            and abs(size - side_size) < 1e-9
            and abs(sc - r.sigma_center) < 1e-9
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
                "otm delta": otm,
                "net vega $": r.net_vega_usd,
                "skew (vp)": skew * 100,
                "size (check/sim)": f"{size:g}/{side_size:g}",
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
    p.add_argument("--out", default="reports/phase5_handcheck.md")
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    df = check(cfg, a.run, a.n, a.seed)
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        cells = [f"{v:.5f}" if isinstance(v, float) else str(v) for v in r.tolist()]
        lines.append("| " + " | ".join(cells) + " |")
    n_ok = int((df["ok"] == "✅").sum())
    md = f"""# Hand-checked fills ({a.run})

Generated by `uv run python -m mm.sim.handcheck --config {a.config} --run {a.run}`.
{len(df)} fills sampled at random (seed {a.seed}) and recomputed from the raw tables with
independent queries and formulas (see the module docstring). **{n_ok}/{len(df)} match.**
Prices in ETH; fair vol decimal. "sweep": the trade was a later leg of one taker order,
so less of our quote was left (size checked against the sweep rule instead).

{chr(10).join(lines)}
"""
    out = Path(a.out)
    out.write_text(md)
    print(f"{n_ok}/{len(df)} fills match -> {out}")


if __name__ == "__main__":
    main()
