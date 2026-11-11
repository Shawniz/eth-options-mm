"""Replay a finished run and attribute its USD P&L (CLAUDE.md P&L attribution, D039).

Replay: the account (B, n_i, Q, P0) is rebuilt from the run's fills, hedges and ledger,
independently of the engine, and checked against the engine's daily snapshots.

Revaluation points: every minute plus every fill, hedge, expiry and funding time.
Between two points the book is fixed and the waterfall, in the order
time -> spot -> basis -> vol, splits the value change:
    theta = V(t1,S0,b0,s0) - V(t0,S0,b0,s0)
    spot  = V(t1,S1,b0,s0) - V(t1,S0,b0,s0) = delta (D x dS, bump at (t1,S0)) + gamma
    basis = V(t1,S1,b1,s0) - V(t1,S1,b0,s0)    futures basis and perp premium
    vega  = V(t1,S1,b1,s1) - V(t1,S1,b1,s0)    the surface refit
At a point, each event's value jump is a flow (USD at that moment):
    fill     spread capture = side q S (fair - price); fee
    hedge    hedge cost = dQ (1/P_exec - 1/P_mark) S; perp fee
    expiry   settlement difference = payoff - model value; delivery fee
    funding  funding
Engine order at one timestamp: expiry, funding -> hedges (perp prints) -> daily snapshot
-> fills. Residual = engine valuation - replay valuation at the daily snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from mm.data.instruments import parse_instrument
from mm.pnl.value import Book, Market, MarketData, delta_eth, value_usd

MIN_MS = 60_000
COMPONENTS = [
    "theta", "delta", "gamma", "basis", "vega", "spread_capture", "hedge_cost",
    "option_fee", "perp_fee", "delivery_fee", "funding", "settlement",
]  # fmt: skip


class _Meta:
    def __init__(self):
        self._c: dict[str, tuple[int, float, bool]] = {}

    def __call__(self, name):
        if name not in self._c:
            i = parse_instrument(name)
            self._c[name] = (i.expiry_ms, float(i.strike_usd), i.option_type == "P")
        return self._c[name]


class Account:
    """The replayed account."""

    def __init__(self, eth: float):
        self.eth, self.q, self.p0 = eth, 0.0, 0.0
        self.pos: dict[str, float] = {}
        self.meta = _Meta()

    def book(self) -> Book:
        names = tuple(sorted(self.pos))
        m = [self.meta(x) for x in names]
        return Book(
            self.eth, self.q, self.p0, names,
            np.array([self.pos[x] for x in names], float),
            np.array([x[0] for x in m], "int64"),
            np.array([x[1] for x in m], float),
            np.array([x[2] for x in m], bool),
        )  # fmt: skip


@dataclass
class Event:
    ts: int
    prio: int  # 0 expiry/funding, 1 hedge, 3 fill
    kind: str
    data: dict


def events_from_run(fills: pd.DataFrame, hedges: pd.DataFrame, ledger: pd.DataFrame) -> list:
    ev = []
    for r in fills.itertuples(index=False):
        ev.append(Event(int(r.ts_ms), 3, "fill", {
            "name": r.instrument_name, "signed": r.qty if r.our_side == "buy" else -r.qty,
            "price": float(r.fill_price), "fee": float(r.fee_eth),
            "edge_eth": float(r.edge_eth)}))  # fmt: skip
    for r in hedges.itertuples(index=False):
        d = {"dq": float(r.dq_usd), "price": float(r.exec_usd), "fee": float(r.fee_eth)}
        ev.append(Event(int(r.exec_ms), 1, "hedge", d))
    led = ledger
    st = led[led["kind"] == "settlement"]
    dfee = led[led["kind"] == "delivery_fee"].set_index(["ts_ms", "ref"])["eth"]
    for r in st.itertuples(index=False):
        ev.append(Event(int(r.ts_ms), 0, "expiry", {
            "name": r.ref, "payoff": float(r.eth),
            "dfee": float(dfee.get((r.ts_ms, r.ref), 0.0))}))  # fmt: skip
    for r in led[led["kind"] == "funding"].itertuples(index=False):
        ev.append(Event(int(r.ts_ms), 0, "funding", {"eth": float(r.eth)}))
    ev.sort(key=lambda e: (e.ts, e.prio))
    return ev


def apply(acct: Account, e: Event) -> None:
    d = e.data
    if e.kind == "fill":
        acct.eth += -d["signed"] * d["price"] - d["fee"]
        n = acct.pos.get(d["name"], 0.0) + d["signed"]
        if abs(n) < 1e-12:
            acct.pos.pop(d["name"], None)
        else:
            acct.pos[d["name"]] = n
    elif e.kind == "hedge":
        if acct.q:
            acct.eth += acct.q * (1 / acct.p0 - 1 / d["price"])
        acct.p0 = d["price"]
        acct.q += d["dq"]
        acct.eth -= d["fee"]
    elif e.kind == "expiry":
        acct.eth += d["payoff"] + d["dfee"]
        acct.pos.pop(d["name"], None)
    elif e.kind == "funding":
        acct.eth += d["eth"]


def waterfall(bk: Book, m0: Market, m1: Market) -> dict:
    v0 = value_usd(bk, m0)
    a = m0.with_(ts=m1.ts)
    v1 = value_usd(bk, a)
    b = a.with_(s=m1.s)
    v2 = value_usd(bk, b)
    c = b.with_(basis=m1.basis, pi=m1.pi)
    v3 = value_usd(bk, c)
    v4 = value_usd(bk, m1)
    dlt = delta_eth(bk, a) * (m1.s - m0.s)
    return {"theta": v1 - v0, "delta": dlt, "gamma": v2 - v1 - dlt, "basis": v3 - v2,
            "vega": v4 - v3, "w0": v0, "w1": v4}  # fmt: skip


def _flow(e: Event, before: float, after: float, s: float) -> dict:
    d, jump = e.data, after - before
    if e.kind == "fill":
        fee = -s * d["fee"]
        return {"spread_capture": jump - fee, "option_fee": fee}
    if e.kind == "hedge":
        fee = -s * d["fee"]
        return {"hedge_cost": jump - fee, "perp_fee": fee}
    if e.kind == "expiry":
        fee = s * d["dfee"]
        return {"settlement": jump - fee, "delivery_fee": fee}
    return {"funding": jump}


@dataclass
class Attribution:
    rows: pd.DataFrame  # one row per interval or event: ts_ms, kind, components, w
    fills: pd.DataFrame  # per fill: spread capture (valuation fair) vs logged edge
    snapshots: pd.DataFrame  # engine vs replay at daily snapshots
    w_start: float
    w_end: float


def attribute(fills, hedges, ledger, daily, md: MarketData, initial_eth: float,
              start_ms: int, end_ms: int) -> Attribution:  # fmt: skip
    ev = events_from_run(fills, hedges, ledger)
    snaps = daily.set_index("ts_ms") if len(daily) else pd.DataFrame()
    grid = np.arange((start_ms // MIN_MS + 1) * MIN_MS, end_ms + 1, MIN_MS, dtype="int64")
    pts = np.unique(np.r_[grid, [e.ts for e in ev], snaps.index.to_numpy("int64"), end_ms])
    pts = pts[(pts > start_ms) & (pts <= end_ms)]
    exps = sorted({int(parse_instrument(n).expiry_ms) for n in fills["instrument_name"]})
    if exps:
        md.preload_basis(np.r_[start_ms, pts], np.array(exps, "int64"))

    acct = Account(initial_eth)
    bk = acct.book()
    m = md.at(start_ms, bk)
    if not np.isfinite(m.s):
        # No index print at or before start_ms (2025-01-01 00:00 is the first instant of
        # the data). Until one arrives the account is collateral only, with no USD value
        # and nothing can trade, so the measurement starts at the first print.
        t_open = md.first_index_ms(start_ms)
        if t_open is None or (ev and ev[0].ts < t_open):
            raise ValueError(f"no index at {start_ms} and events before the first print")
        m = md.at(t_open, bk)
        pts = pts[pts > t_open]
    w_start = value_usd(bk, m)
    rows, fill_rows, snap_rows = [], [], []
    j = 0
    for t in pts:
        t = int(t)
        m1 = md.at(t, bk, prev=m)
        wf = waterfall(bk, m, m1)
        if not all(np.isfinite(wf[k]) for k in ("theta", "delta", "gamma", "basis", "vega")):
            raise ValueError(f"non-finite waterfall step at {t}")  # never read NaN as 0
        rows.append({"ts_ms": t, "kind": "market", **{k: wf[k] for k in
                     ("theta", "delta", "gamma", "basis", "vega")}, "w": wf["w1"]})  # fmt: skip
        m = m1
        snap_done = False
        while j < len(ev) and ev[j].ts == t:
            e = ev[j]
            if e.prio > 2 and not snap_done and t in snaps.index:
                snap_rows.append(_snap(t, acct, bk, m, snaps))
                snap_done = True
            before = value_usd(bk, m)
            apply(acct, e)
            bk = acct.book()
            m = md.at(t, bk)
            after = value_usd(bk, m)
            fl = _flow(e, before, after, m.s)
            rows.append({"ts_ms": t, "kind": e.kind, **fl, "w": after})
            if e.kind == "fill":
                fill_rows.append({"ts_ms": t, "name": e.data["name"],
                                  "spread_capture": fl["spread_capture"],
                                  "edge_logged_usd": e.data["edge_eth"] * m.s})  # fmt: skip
            j += 1
        if not snap_done and t in snaps.index:
            snap_rows.append(_snap(t, acct, bk, m, snaps))
    out = pd.DataFrame(rows)
    for c in COMPONENTS:
        if c not in out:
            out[c] = 0.0
    out[COMPONENTS] = out[COMPONENTS].fillna(0.0)
    return Attribution(out, pd.DataFrame(fill_rows), pd.DataFrame(snap_rows), w_start,
                       float(out["w"].iloc[-1]) if len(out) else w_start)  # fmt: skip


def _snap(t, acct: Account, bk: Book, m: Market, snaps) -> dict:
    s = snaps.loc[t]
    return {
        "ts_ms": t,
        "replay_w": value_usd(bk, m),
        "engine_w": float(s["equity_usd"]),
        "replay_eth": acct.eth,
        "engine_eth": float(s["eth_balance"]),
        "replay_perp_usd": acct.q,
        "engine_perp_usd": float(s["perp_usd"]),
        "replay_n": len(acct.pos),
        "engine_n": int(s["n_positions"]),
    }
