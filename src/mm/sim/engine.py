"""Event loop: real option trades fill a quoter; the perpetual hedges; fees, expiry
settlement, funding.

Events in time order; at the same timestamp: settlement and funding (0) -> perpetual
prints (1, the underlying update) -> daily snapshot (2) -> option trades (3), each stream
in exchange order. Surface refits are read from the Phase 3 cache through fair.py
(refit -> quote order). A hedge check follows every perp print and every option fill
(underlying -> refit -> quote and fill -> hedge, CLAUDE.md). A hedge decided at a print
or fill executes at the first same-side perp print strictly after it with
ts >= decision + latency (D036).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np
import pandas as pd

from mm.config import Config
from mm.data.instruments import parse_instrument
from mm.hedging.hedger import exec_price, hedge_qty, needs_hedge, perp_fee_eth
from mm.pricing.black76 import price_eth
from mm.pricing.forwards import ForwardCurve
from mm.pricing.time import MS_PER_YEAR
from mm.quoting.quoter import apply_limits, in_universe, make_quote
from mm.risk.greeks import book_gamma1_usd, book_vega_usd, perp_value_eth
from mm.risk.limits import side_room
from mm.sim.account import Account
from mm.sim.delta import OptionDelta
from mm.sim.fair import SurfaceBook, svi_total_variance
from mm.sim.fees import delivery_fee_eth, funding_eth, is_daily_expiry, option_fee_eth, payoff_eth
from mm.sim.fills import QuoteBook, decide

DAY_MS = 86_400_000


@dataclass
class SimInputs:
    trades: pd.DataFrame  # option trades with fair_values() columns, any order
    book: SurfaceBook
    curve: ForwardCurve
    delivery_usd: dict[int, float]  # settle_ms -> delivery price
    funding: pd.DataFrame  # available_ms, interest_1h, index_price_usd
    activity_ms: np.ndarray  # timestamps of every trade on every stream (pauses)
    start_ms: int
    end_ms: int
    perp: pd.DataFrame | None = None  # ts_ms, direction, price_usd, mark_price_usd, index_price_usd


@dataclass
class SimResult:
    fills: pd.DataFrame
    ledger: pd.DataFrame
    daily: pd.DataFrame
    skips: Counter
    account: Account
    pauses: pd.DataFrame
    hedges: pd.DataFrame
    delta: pd.DataFrame  # account delta at every hedge check
    quotes: pd.DataFrame  # every quote that reached the limit stage
    limits: Counter


def find_pauses(activity_ms: np.ndarray, gap_s: float, grid: np.ndarray) -> pd.DataFrame:
    """Gaps with no trade on any stream longer than gap_s. Quoting stays off from the
    end of the gap until the first refit after it (inputs are stale until then)."""
    t = np.unique(np.asarray(activity_ms, dtype="int64"))
    d = np.diff(t)
    i = np.flatnonzero(d > gap_s * 1000)
    ends = t[i + 1]
    j = np.searchsorted(grid, ends, side="right")
    resume = np.where(j < len(grid), grid[np.clip(j, 0, len(grid) - 1)], np.iinfo("int64").max)
    return pd.DataFrame({"gap_start_ms": t[i], "gap_end_ms": ends, "quote_from_ms": resume})


def _blocked(ts_ms: np.ndarray, pauses: pd.DataFrame) -> np.ndarray:
    out = np.zeros(len(ts_ms), bool)
    for ge, qf in zip(pauses["gap_end_ms"], pauses["quote_from_ms"], strict=True):
        out |= (ts_ms >= ge) & (ts_ms < qf)
    return out


class _Meta:
    def __init__(self):
        self._c: dict[str, tuple[int, float, bool]] = {}

    def __call__(self, name: str):
        if name not in self._c:
            i = parse_instrument(name)
            self._c[name] = (i.expiry_ms, float(i.strike_usd), i.option_type == "C")
        return self._c[name]


def _value(acct: Account, ts_ms: int, inp: SimInputs, meta: _Meta, mark, delta) -> dict:
    """Account value at Surface A (CLAUDE.md 13): W = S (B + sum n_i C_i/F_i + perp),
    perp = Q (1/P0 - 1/mark) at the latest perp mark. Valuation is measurement, so it uses
    information up to and including ts (D039)."""
    r = int(inp.book.latest_refit(np.array([ts_ms]))[0])
    opt_eth, missing = 0.0, 0
    index = inp.curve.index_at(ts_ms, inclusive=True)
    for name, n in acct.positions.items():
        exp, k, call = meta(name)
        fw = inp.curve.forwards(
            pd.Series([pd.Timestamp(ts_ms, unit="ms", tz="UTC")]),
            pd.Series([pd.Timestamp(exp, unit="ms", tz="UTC")]), 0, inclusive=True,
        ).iloc[0]  # fmt: skip
        sl = inp.book.slice_at_or_before(exp, r)
        if sl is None or not np.isfinite(fw.basis):
            missing += 1
            continue
        f = fw.basis * index
        kk = np.log(k / f)
        w = svi_total_variance(*(sl[c] for c in ("a", "b", "rho", "m", "s")), kk)
        sig = float(np.sqrt(max(w, 0.0) / sl["t_ref"]))
        t = (exp - ts_ms) / MS_PER_YEAR
        opt_eth += n * float(price_eth(f, k, t, sig, call))
    perp_eth = perp_value_eth(acct.perp_usd, acct.perp_entry, mark) if acct.perp_usd else 0.0
    eq = acct.eth + opt_eth + perp_eth
    return {
        "ts_ms": ts_ms,
        "eth_balance": acct.eth,
        "options_eth": opt_eth,
        "perp_eth": perp_eth,
        "equity_eth": eq,
        "index_usd": index,
        "equity_usd": index * eq,
        "perp_usd": acct.perp_usd,
        "delta_eth": delta,
        "n_positions": len(acct.positions),
        "n_unvalued": missing,
    }


def _book_greeks(acct: Account, refit_ms: int, inp: SimInputs, meta: _Meta):
    """Net vega (USD per vol point) and net cash gamma (USD per 1% move squared) of the
    option book at refit R, from information before R only: each expiry's latest live
    slice at or before R, forwards strictly < R."""
    names = [n for n, q in acct.positions.items() if q]
    if not names:
        return 0.0, 0.0
    m = [meta(n) for n in names]
    exp = np.array([x[0] for x in m], "int64")
    k = np.array([x[1] for x in m])
    ts = pd.Series(pd.to_datetime(np.full(len(names), refit_ms), unit="ms", utc=True))
    fw = inp.curve.forwards(ts, pd.Series(pd.to_datetime(exp, unit="ms", utc=True)), 0)
    f, s = fw["forward_usd"].to_numpy(), fw["index_usd"].to_numpy()
    sig = np.full(len(names), np.nan)
    for i, e in enumerate(exp):
        sl = inp.book.slice_at_or_before(int(e), refit_ms)
        if sl is not None and np.isfinite(f[i]):
            w = svi_total_variance(*(sl[c] for c in ("a", "b", "rho", "m", "s")),
                                   np.log(k[i] / f[i]))  # fmt: skip
            sig[i] = np.sqrt(max(w, 0.0) / sl["t_ref"])
    t = (exp - refit_ms) / MS_PER_YEAR
    n = np.array([acct.positions[x] for x in names])
    return book_vega_usd(n, f, k, t, sig, s), book_gamma1_usd(n, f, k, t, sig, s)


def run(inp: SimInputs, cfg: Config) -> SimResult:
    spec, qcfg, fcfg, sc = cfg.specs.option, cfg.quoting, cfg.fill, cfg.sim
    hcfg, rcfg, pspec = cfg.hedging, cfg.risk, cfg.specs.perp
    acct = Account(sc.initial_eth)
    qbook = QuoteBook(sc.latency_ms)
    meta = _Meta()
    odelta = OptionDelta(inp.book, inp.curve, meta, cfg.hedging.delta)
    skips: Counter = Counter()
    lim: Counter = Counter()
    fills, daily, hedges, quotes = [], [], [], []
    # account delta at every hedge check (one per perp print or fill at most), preallocated
    n_chk = (0 if inp.perp is None else len(inp.perp)) + len(inp.trades) + 16
    d_ts = np.empty(n_chk, "int64")
    d_val = np.empty(n_chk)
    n_d = [0]

    tr = inp.trades
    tr = tr[(tr["ts_ms"] >= inp.start_ms) & (tr["ts_ms"] < inp.end_ms)]
    # exchange order: ts, then Deribit's global trade number (D044)
    tr = tr.assign(_no=tr["trade_id"].astype(str).str.extract(r"(\d+)$")[0].astype("int64"))
    tr = tr.sort_values(["ts_ms", "_no"], kind="stable").reset_index(drop=True)
    pauses = find_pauses(inp.activity_ms, sc.pause_gap_s, inp.book.grid)
    blocked = _blocked(tr["ts_ms"].to_numpy(), pauses)

    pp_df = inp.perp if inp.perp is not None else pd.DataFrame(
        columns=["ts_ms", "direction", "price_usd", "mark_price_usd", "index_price_usd"]
    )  # fmt: skip
    pp_df = pp_df[(pp_df["ts_ms"] >= inp.start_ms) & (pp_df["ts_ms"] < inp.end_ms)]
    p_ts = pp_df["ts_ms"].to_numpy("int64")
    p_buy = (pp_df["direction"] == "buy").to_numpy()
    p_px = pp_df["price_usd"].to_numpy(float)
    p_mark = pp_df["mark_price_usd"].to_numpy(float)
    p_idx = pp_df["index_price_usd"].to_numpy(float)
    n_perp = len(p_ts)

    # non-trade events: (ts, priority, kind, payload); perp prints (priority 1) merge in
    ev = [(int(e), 0, "expiry", None) for e in np.unique(tr["exp_ms"]) if e < inp.end_ms]
    f = inp.funding
    f = f[(f["available_ms"] >= inp.start_ms) & (f["available_ms"] < inp.end_ms)]
    ev += [
        (int(a), 0, "funding", (float(r), float(i)))
        for a, r, i in zip(f["available_ms"], f["interest_1h"], f["index_price_usd"], strict=True)
    ]
    first_day = (inp.start_ms // DAY_MS + 1) * DAY_MS
    ev += [(int(d), 2, "snapshot", None) for d in range(first_day, inp.end_ms + 1, DAY_MS)]
    if inp.end_ms % DAY_MS:
        ev.append((inp.end_ms, 2, "snapshot", None))
    ev.sort(key=lambda x: (x[0], x[1]))
    st = {"ev": 0, "pp": 0, "S": np.nan, "mark": np.nan, "pending": None}
    # net book vega and cash gamma, recomputed at each refit, updated by fills
    vega_state = {"refit": None, "net": 0.0, "gamma": 0.0}

    def account_delta(ts: int) -> float:
        d = acct.eth + odelta.total(acct, ts, st["S"])
        return d + (acct.perp_usd / acct.perp_entry if acct.perp_usd else 0.0)

    def hedge_check(ts: int) -> None:
        """Decide a hedge (if none pending). Trigger = last perp print processed."""
        if st["pending"] is not None or not np.isfinite(st["S"]):
            return
        d = account_delta(ts)
        d_ts[n_d[0]], d_val[n_d[0]] = ts, d
        n_d[0] += 1
        if needs_hedge(d, hcfg.delta_band_eth):
            dq = hedge_qty(d, st["S"], pspec.contract_size_usd)
            if abs(dq) >= pspec.min_order_usd:
                st["pending"] = {"dq": dq, "ready": ts + sc.latency_ms, "after": st["pp"] - 1,
                                 "decide_ms": ts, "delta": d, "S": st["S"]}  # fmt: skip

    def on_perp(j: int) -> None:
        ts = int(p_ts[j])
        st["S"], st["mark"] = p_idx[j], p_mark[j]
        h = st["pending"]
        if h is not None and j > h["after"] and ts >= h["ready"] and p_buy[j] == (h["dq"] > 0):
            sign = 1 if h["dq"] > 0 else -1
            px = exec_price(p_px[j], sign, hcfg.hedge_cost_bps)
            fee = perp_fee_eth(h["dq"], px, pspec)
            q_before = acct.perp_usd
            acct.perp_trade(ts, h["dq"], px, fee)
            acct.check()
            st["pending"] = None
            hedges.append({
                "decide_ms": h["decide_ms"], "exec_ms": ts, "wait_ms": ts - h["decide_ms"],
                "delta_before": h["delta"], "index_at_decision": h["S"], "dq_usd": h["dq"],
                "q_before_usd": q_before, "q_after_usd": acct.perp_usd, "print_usd": p_px[j],
                "exec_usd": px, "fee_eth": fee,
                # cost of the bps vs the print, in ETH: dQ (1/print - 1/exec) >= 0
                "slippage_eth": h["dq"] * (1 / p_px[j] - 1 / px),
            })  # fmt: skip
        hedge_check(ts)

    def handle(e):
        ts, _, kind, payload = e
        if kind == "expiry":
            px = inp.delivery_usd.get(ts)
            for name in [n for n in acct.positions if meta(n)[0] == ts]:
                if px is None:
                    raise KeyError(f"no delivery price for expiry {ts}")
                _, k, call = meta(name)
                n = acct.positions[name]
                pay = payoff_eth(k, call, px)
                acct.post(ts, "settlement", n * pay, name)
                acct.post(
                    ts, "delivery_fee", -delivery_fee_eth(n, pay, spec, is_daily_expiry(ts)), name
                )
                acct.move(ts, "expiry", name, -n)
            vega_state["refit"] = None  # positions gone: recompute at the next trade
            odelta.invalidate()
        elif kind == "funding":
            rate, index = payload
            if acct.perp_usd:
                acct.post(ts, "funding", funding_eth(acct.perp_usd, rate, index), "perp")
        elif kind == "snapshot":
            dl = account_delta(ts) if np.isfinite(st["S"]) else np.nan
            daily.append(_value(acct, ts, inp, meta, st["mark"], dl))
        acct.check()

    def advance(until: int) -> None:
        """Process events and perp prints with ts <= until, in (ts, priority) order."""
        while True:
            te = ev[st["ev"]][0] if st["ev"] < len(ev) else None
            tp = int(p_ts[st["pp"]]) if st["pp"] < n_perp else None
            e_ok = te is not None and te <= until
            p_ok = tp is not None and tp <= until
            if not (e_ok or p_ok):
                return
            if e_ok and (not p_ok or te < tp or (te == tp and ev[st["ev"]][1] < 1)):
                handle(ev[st["ev"]])
                st["ev"] += 1
            else:
                st["pp"] += 1
                on_perp(st["pp"] - 1)

    cols = [
        "ts_ms", "trade_id", "trade_seq", "instrument_name", "direction", "price_eth",
        "amount_contracts", "exp_ms", "strike_usd", "option_type", "t_years", "fit_kind",
        "refit_ms", "forward_q", "index_q", "sigma_fair", "fair_eth", "is_suspect",
        "is_liquidation", "otm_delta", "vega_usd", "gamma1_usd",
    ]  # fmt: skip
    for i, row in enumerate(tr[cols].itertuples(index=False)):
        advance(row.ts_ms)
        days = row.t_years * 365
        u = qcfg.universe
        if not (u.min_days <= days <= u.max_days):
            skips["tenor"] += 1
            continue
        if blocked[i]:
            skips["paused"] += 1
            continue
        if not isinstance(row.fit_kind, str):
            skips["no_surface"] += 1
            continue
        if not np.isfinite(row.forward_q) or not np.isfinite(row.sigma_fair):
            skips["no_forward"] += 1
            continue
        if not in_universe(row.otm_delta, days, u):
            skips["low_delta"] += 1
            continue
        if vega_state["refit"] != row.refit_ms:
            vega_state["refit"] = row.refit_ms
            vega_state["net"], vega_state["gamma"] = _book_greeks(acct, int(row.refit_ms), inp,
                                                                  meta)  # fmt: skip
        call = row.option_type == "C"
        v_net = vega_state["net"]
        q0 = make_quote(row.sigma_fair, row.forward_q, row.strike_usd, row.t_years, call,
                        v_net, qcfg, spec, rcfg.vega_limit_usd, row.index_q)  # fmt: skip
        if q0 is None:
            skips["no_size"] += 1
            continue
        pos_before = acct.positions.get(row.instrument_name, 0.0)
        bid_room, ask_room = side_room(vega_state["net"], row.vega_usd, pos_before, rcfg,
                                       vega_state["gamma"], row.gamma1_usd)  # fmt: skip
        q = apply_limits(q0, bid_room, ask_room, spec)
        for side, before, after in (("bid", q0.bid, q.bid), ("ask", q0.ask, q.ask)):
            if before is None:
                continue
            size_after = q.bid_size if side == "bid" else q.ask_size
            if after is None:
                lim[f"{side}_dropped"] += 1
            elif size_after < q0.size:
                lim[f"{side}_cut"] += 1
        lim["quotes"] += 1
        quotes.append((row.ts_ms, vega_state["net"], vega_state["gamma"], q0.size, q.bid_size,
                       q.ask_size))  # fmt: skip
        if q.bid is None and q.ask is None:
            skips["limit"] += 1
            continue
        rng = (
            np.random.default_rng([sc.seed, int(row.trade_seq)])
            if sc.fill_sizing == "random"
            else None
        )
        d = decide(row.instrument_name, row.direction, row.price_eth, row.amount_contracts,
                   row.ts_ms, q.bid, q.ask, (q.bid_size, q.ask_size), qbook, fcfg,
                   sc.fill_sizing, rng)  # fmt: skip
        if d is None:
            skips["no_cross"] += 1
            continue
        fee = option_fee_eth(d.qty, d.price, spec, maker=True)
        acct.option_fill(row.ts_ms, row.instrument_name, d.our_side, d.qty, d.price, fee)
        acct.check()
        signed = d.qty if d.our_side == "buy" else -d.qty
        net_before, gamma_before = vega_state["net"], vega_state["gamma"]
        vega_state["net"] += signed * row.vega_usd
        vega_state["gamma"] += signed * row.gamma1_usd
        odelta.invalidate()
        fills.append(
            {
                **row._asdict(),
                "bid": q.bid,
                "ask": q.ask,
                "quote_size": q0.size,
                "bid_size": q.bid_size,
                "ask_size": q.ask_size,
                "pos_before": pos_before,
                "sigma_center": q.sigma_center,
                "skew_volpts": q.skew_volpts,
                "skew_capped": q.skew_capped,
                "floor_bound": q.floor_bound,
                "net_vega_usd": net_before,
                "net_gamma1_usd": gamma_before,
                "our_side": d.our_side,
                "fill_price": d.price,
                "eligible": d.eligible,
                "qty": d.qty,
                "fee_eth": fee,
                "edge_eth": signed * (row.fair_eth - d.price),
                "reason": d.reason,
            }
        )
        hedge_check(row.ts_ms)  # quote and fill -> hedge
    advance(np.iinfo("int64").max)
    acct.audit()

    fills_df = pd.DataFrame(fills)
    if len(fills_df):
        bad = fills_df["reason"].isna() | (fills_df["reason"] == "")
        if bad.any():
            raise AssertionError(f"{int(bad.sum())} fills without a reason")
    ledger = pd.DataFrame(acct.ledger, columns=["ts_ms", "kind", "ref", "eth"])
    hedge_cols = ["decide_ms", "exec_ms", "wait_ms", "delta_before", "index_at_decision",
                  "dq_usd", "q_before_usd", "q_after_usd", "print_usd", "exec_usd", "fee_eth",
                  "slippage_eth"]  # fmt: skip
    return SimResult(
        fills_df,
        ledger,
        pd.DataFrame(daily),
        skips,
        acct,
        pauses,
        pd.DataFrame(hedges, columns=hedge_cols),
        pd.DataFrame({"ts_ms": d_ts[: n_d[0]], "delta_eth": d_val[: n_d[0]]}),
        pd.DataFrame(
            quotes,
            columns=["ts_ms", "net_vega_usd", "net_gamma1_usd", "size", "bid_size", "ask_size"],
        ),  # fmt: skip
        lim,
    )
