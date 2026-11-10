"""Event loop: real option trades fill a quoter; fees, expiry settlement, funding.

Events in time order; at the same timestamp: settlement and funding (0) -> daily
snapshot (1) -> option trades (2), the trades in exchange order (ts, trade_seq).
Surface refits are read from the Phase 3 cache through fair.py (refit -> quote order).
Perpetual trades are not events yet: the underlying comes from the forward curve. They
join the loop in Phase 6 (hedging).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np
import pandas as pd

from mm.config import Config
from mm.data.instruments import parse_instrument
from mm.pricing.black76 import price_eth
from mm.pricing.forwards import ForwardCurve
from mm.pricing.time import MS_PER_YEAR
from mm.quoting.quoter import in_universe, make_quote
from mm.risk.greeks import book_vega_usd
from mm.sim.account import Account
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


@dataclass
class SimResult:
    fills: pd.DataFrame
    ledger: pd.DataFrame
    daily: pd.DataFrame
    skips: Counter
    account: Account
    pauses: pd.DataFrame


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


def _value(acct: Account, ts_ms: int, inp: SimInputs, meta: _Meta) -> dict:
    """Account value at Surface A (CLAUDE.md 13): W = S (B + sum n_i C_i/F_i)."""
    r = int(inp.book.latest_refit(np.array([ts_ms]))[0])
    opt_eth, missing = 0.0, 0
    index = np.nan
    for name, n in acct.positions.items():
        exp, k, call = meta(name)
        fw = inp.curve.forward(
            pd.Timestamp(exp, unit="ms", tz="UTC"), pd.Timestamp(ts_ms, unit="ms", tz="UTC"), 0
        )
        index = fw.index_usd
        sl = inp.book.slice_at_or_before(exp, r)
        if sl is None or not np.isfinite(fw.forward_usd):
            missing += 1
            continue
        kk = np.log(k / fw.forward_usd)
        w = svi_total_variance(*(sl[c] for c in ("a", "b", "rho", "m", "s")), kk)
        sig = float(np.sqrt(max(w, 0.0) / sl["t_ref"]))
        t = (exp - ts_ms) / MS_PER_YEAR
        opt_eth += n * float(price_eth(fw.forward_usd, k, t, sig, call))
    if not np.isfinite(index):
        index = inp.curve.index_at(ts_ms)
    return {
        "ts_ms": ts_ms,
        "eth_balance": acct.eth,
        "options_eth": opt_eth,
        "equity_eth": acct.eth + opt_eth,
        "index_usd": index,
        "equity_usd": index * (acct.eth + opt_eth),
        "n_positions": len(acct.positions),
        "n_unvalued": missing,
    }


def _book_vega(acct: Account, refit_ms: int, inp: SimInputs, meta: _Meta) -> float:
    """Net vega (USD per vol point) of the option book at refit R, from information
    before R only: each expiry's latest live slice at or before R, forwards strictly < R."""
    names = [n for n, q in acct.positions.items() if q]
    if not names:
        return 0.0
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
    return book_vega_usd(n, f, k, t, sig, s)


def run(inp: SimInputs, cfg: Config) -> SimResult:
    spec, qcfg, fcfg, sc = cfg.specs.option, cfg.quoting, cfg.fill, cfg.sim
    acct = Account(sc.initial_eth)
    qbook = QuoteBook(sc.latency_ms)
    meta = _Meta()
    skips: Counter = Counter()
    fills, daily = [], []

    tr = inp.trades
    tr = tr[(tr["ts_ms"] >= inp.start_ms) & (tr["ts_ms"] < inp.end_ms)]
    tr = tr.sort_values(["ts_ms", "trade_seq"], kind="stable").reset_index(drop=True)
    pauses = find_pauses(inp.activity_ms, sc.pause_gap_s, inp.book.grid)
    blocked = _blocked(tr["ts_ms"].to_numpy(), pauses)

    # non-trade events: (ts, priority, kind, payload)
    ev = [(int(e), 0, "expiry", None) for e in np.unique(tr["exp_ms"]) if e < inp.end_ms]
    f = inp.funding
    f = f[(f["available_ms"] >= inp.start_ms) & (f["available_ms"] < inp.end_ms)]
    ev += [
        (int(a), 0, "funding", (float(r), float(i)))
        for a, r, i in zip(f["available_ms"], f["interest_1h"], f["index_price_usd"], strict=True)
    ]
    first_day = (inp.start_ms // DAY_MS + 1) * DAY_MS
    ev += [(int(d), 1, "snapshot", None) for d in range(first_day, inp.end_ms + 1, DAY_MS)]
    if inp.end_ms % DAY_MS:
        ev.append((inp.end_ms, 1, "snapshot", None))
    ev.sort(key=lambda x: (x[0], x[1]))
    pos = 0
    vega_state = {"refit": None, "net": 0.0}  # net book vega, recomputed at each refit

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
        elif kind == "funding":
            rate, index = payload
            if acct.perp_usd:
                acct.post(ts, "funding", funding_eth(acct.perp_usd, rate, index), "perp")
        elif kind == "snapshot":
            daily.append(_value(acct, ts, inp, meta))
        acct.check()

    cols = [
        "ts_ms", "trade_id", "trade_seq", "instrument_name", "direction", "price_eth",
        "amount_contracts", "exp_ms", "strike_usd", "option_type", "t_years", "fit_kind",
        "refit_ms", "forward_q", "index_q", "sigma_fair", "fair_eth", "is_suspect",
        "is_liquidation", "otm_delta", "vega_usd",
    ]  # fmt: skip
    for i, row in enumerate(tr[cols].itertuples(index=False)):
        while pos < len(ev) and ev[pos][0] <= row.ts_ms:
            handle(ev[pos])
            pos += 1
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
            vega_state["net"] = _book_vega(acct, int(row.refit_ms), inp, meta)
        call = row.option_type == "C"
        q = make_quote(row.sigma_fair, row.forward_q, row.strike_usd, row.t_years, call,
                       vega_state["net"], qcfg, spec, row.index_q)  # fmt: skip
        if q is None:
            skips["no_size"] += 1
            continue
        rng = (
            np.random.default_rng([sc.seed, int(row.trade_seq)])
            if sc.fill_sizing == "random"
            else None
        )
        d = decide(row.instrument_name, row.direction, row.price_eth, row.amount_contracts,
                   row.ts_ms, q.bid, q.ask, q.size, qbook, fcfg, sc.fill_sizing, rng)  # fmt: skip
        if d is None:
            skips["no_cross"] += 1
            continue
        fee = option_fee_eth(d.qty, d.price, spec, maker=True)
        acct.option_fill(row.ts_ms, row.instrument_name, d.our_side, d.qty, d.price, fee)
        acct.check()
        signed = d.qty if d.our_side == "buy" else -d.qty
        net_before = vega_state["net"]
        vega_state["net"] += signed * row.vega_usd
        fills.append(
            {
                **row._asdict(),
                "bid": q.bid,
                "ask": q.ask,
                "quote_size": q.size,
                "sigma_center": q.sigma_center,
                "skew_volpts": q.skew_volpts,
                "skew_capped": q.skew_capped,
                "floor_bound": q.floor_bound,
                "net_vega_usd": net_before,
                "our_side": d.our_side,
                "fill_price": d.price,
                "eligible": d.eligible,
                "qty": d.qty,
                "fee_eth": fee,
                "edge_eth": signed * (row.fair_eth - d.price),
                "reason": d.reason,
            }
        )
    while pos < len(ev):
        handle(ev[pos])
        pos += 1
    acct.audit()

    fills_df = pd.DataFrame(fills)
    if len(fills_df):
        bad = fills_df["reason"].isna() | (fills_df["reason"] == "")
        if bad.any():
            raise AssertionError(f"{int(bad.sum())} fills without a reason")
    ledger = pd.DataFrame(acct.ledger, columns=["ts_ms", "kind", "ref", "eth"])
    return SimResult(fills_df, ledger, pd.DataFrame(daily), skips, acct, pauses)
