"""Per-fill markouts at fixed horizons (CLAUDE.md P&L attribution, D039).

The fill's own contribution to the USD account is W_f(t') = side q S(t') (fair(t') - p):
we paid (or received) p ETH, whose USD value also moves with S. So
    raw(h)    = side q [ S(t+h) fair(t+h) - S(t+h) p ]
    hedged(h) = raw(h) - side q (delta - p) (S(t+h) - S(t))
with delta the smile-adjusted delta at the fill (D038); delta - p is the fill's account
delta (the premium sits in B). At h = 0 both equal the spread capture. fair is Surface A
on the inclusive state (D039). A horizon past expiry or past the data end is NaN.
Also reported per $ of vega: markout / (q x vega_usd) is in vol points.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from mm.data.instruments import parse_instrument
from mm.pnl.value import Book, MarketData, option_eth, smile_delta_per_contract

HORIZONS = {"0": 0, "1min": 60_000, "5min": 300_000, "1h": 3_600_000}


def _one(name: str) -> Book:
    i = parse_instrument(name)
    return Book(0.0, 0.0, 0.0, (name,), np.array([1.0]), np.array([i.expiry_ms], "int64"),
                np.array([float(i.strike_usd)]), np.array([i.option_type == "P"]))  # fmt: skip


def markouts(fills: pd.DataFrame, md: MarketData, end_ms: int) -> pd.DataFrame:
    if fills.empty:
        return pd.DataFrame()
    books = {n: _one(n) for n in fills["instrument_name"].unique()}
    ts = fills["ts_ms"].to_numpy("int64")
    exp = np.array([books[n].exp[0] for n in fills["instrument_name"]], "int64")
    md.preload_pairs(
        np.concatenate([ts + h for h in HORIZONS.values()]), np.tile(exp, len(HORIZONS))
    )
    side = np.where(fills["our_side"] == "buy", 1.0, -1.0)
    q, p = fills["qty"].to_numpy(float), fills["fill_price"].to_numpy(float)
    out = {"ts_ms": ts, "instrument_name": fills["instrument_name"].to_numpy(),
           "our_side": fills["our_side"].to_numpy(), "qty": q,
           "days": fills["t_years"].to_numpy(float) * 365,
           "otm_delta": fills["otm_delta"].to_numpy(float),
           "vega_usd": fills["vega_usd"].to_numpy(float)}  # fmt: skip
    s0 = np.empty(len(ts))
    d0 = np.empty(len(ts))
    for i, (t, name) in enumerate(zip(ts, fills["instrument_name"], strict=True)):
        bk = books[name]
        m = md.at(int(t), bk)
        s0[i] = m.s
        d0[i] = smile_delta_per_contract(bk, m)[0]
    out["delta"] = d0
    for lab, h in HORIZONS.items():
        raw = np.full(len(ts), np.nan)
        hed = np.full(len(ts), np.nan)
        for i, (t, name) in enumerate(zip(ts, fills["instrument_name"], strict=True)):
            th = int(t) + h
            if th >= exp[i] or th > end_ms:
                continue
            bk = books[name]
            m = md.at(th, bk)
            fair = option_eth(bk, m)[0]
            raw[i] = side[i] * q[i] * m.s * (fair - p[i])
            hed[i] = raw[i] - side[i] * q[i] * (d0[i] - p[i]) * (m.s - s0[i])
        out[f"raw_{lab}"] = raw
        out[f"hedged_{lab}"] = hed
    return pd.DataFrame(out)


EXPIRY_BUCKETS = [2, 8, 35, 90]
DELTA_BUCKETS = [0.10, 0.20, 0.35, 0.5001]


def markout_table(mk: pd.DataFrame, by: str) -> pd.DataFrame:
    """Sum of delta-hedged markouts (USD) and per $ of vega (vol points) by bucket."""
    g = mk.copy()
    if by == "expiry":
        g["bucket"] = pd.cut(g["days"], EXPIRY_BUCKETS, right=False)
    else:
        g["bucket"] = pd.cut(g["otm_delta"], DELTA_BUCKETS, right=False)
    g["bucket"] = g["bucket"].cat.rename_categories(str)  # ordered by edge, printable
    vega = (g["qty"] * g["vega_usd"]).groupby(g["bucket"], observed=True).sum()
    rows = {"fills": g.groupby("bucket", observed=True).size()}
    for lab in HORIZONS:
        s = g.groupby("bucket", observed=True)[f"hedged_{lab}"].sum()
        rows[f"{lab} $"] = s
        rows[f"{lab} vp"] = s / vega
    return pd.DataFrame(rows)
