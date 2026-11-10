"""Market state and USD account valuation for P&L attribution (CLAUDE.md, D039).

Valuation is measurement, not a decision, so it uses information UP TO AND INCLUDING t
(D018 inclusive rule): the index at or before t, the latest refit at or before t, the
forward basis at or before t, the perp mark of the latest print at or before t.

    W = S [ B + sum n_i C_i/F_i + Q (1/P0 - 1/P) ],   F_i = basis_i S,   P = pi S
with C_i/F_i from Black-76 at the slice's vol at the current moneyness (sticky moneyness,
D029). A market state m = (t, S, basis, pi, slices); the waterfall swaps its fields one
at a time.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd
from scipy.special import ndtr

from mm.pricing.black76 import price_eth
from mm.pricing.time import MS_PER_YEAR

RAW = ("a", "b", "rho", "m", "s")


@dataclass(frozen=True)
class Book:
    """Account at one moment. Option arrays are aligned (one entry per instrument)."""

    eth: float
    perp_usd: float
    perp_entry: float
    names: tuple[str, ...]
    n: np.ndarray
    exp: np.ndarray  # int64 ms
    k: np.ndarray
    put: np.ndarray  # bool


@dataclass(frozen=True)
class Market:
    """Market inputs aligned with a Book's instruments."""

    ts: int
    s: float
    pi: float  # perp mark / index
    basis: np.ndarray  # F / index, per instrument
    par: np.ndarray  # (n, 6): a, b, rho, m, s, t_ref per instrument

    def with_(self, **kw) -> Market:
        return replace(self, **kw)


def _sigma(par: np.ndarray, k: np.ndarray, f: np.ndarray) -> np.ndarray:
    a, b, rho, m, s, tref = par.T
    x = np.log(k / f) - m
    w = a + b * (rho * x + np.sqrt(x * x + s * s))
    return np.sqrt(np.maximum(w, 0.0) / tref)


def option_eth(bk: Book, mk: Market) -> np.ndarray:
    """C_i / F_i per contract (ETH). At or after expiry: intrinsic at F."""
    if not len(bk.n):
        return np.zeros(0)
    f = mk.basis * mk.s
    t = (bk.exp - mk.ts) / MS_PER_YEAR
    live = t > 0
    out = np.where(bk.put, np.maximum(bk.k - f, 0.0), np.maximum(f - bk.k, 0.0)) / f
    if live.any():
        sig = _sigma(mk.par[live], bk.k[live], f[live])
        out[live] = price_eth(f[live], bk.k[live], t[live], sig, ~bk.put[live])
    return out


def value_usd(bk: Book, mk: Market) -> float:
    opt = float(np.dot(bk.n, option_eth(bk, mk))) if len(bk.n) else 0.0
    perp = bk.perp_usd * (1 / bk.perp_entry - 1 / (mk.pi * mk.s)) if bk.perp_usd else 0.0
    return mk.s * (bk.eth + opt + perp)


def delta_eth(bk: Book, mk: Market, rel: float = 1e-4) -> float:
    """dW/dS by central bump of the whole account (perp price moves with S)."""
    h = mk.s * rel
    up, dn = value_usd(bk, mk.with_(s=mk.s + h)), value_usd(bk, mk.with_(s=mk.s - h))
    return (up - dn) / (2 * h)


def smile_delta_per_contract(bk: Book, mk: Market, mode: str = "smile") -> np.ndarray:
    """Per-instrument hedge delta: N(d1) [-1 puts], minus phi(d1) sqrt(T) sigma'(k) when
    mode = "smile" (D038). The hedger's mode is config hedging.delta (D041)."""
    f = mk.basis * mk.s
    a, b, rho, m, s, tref = mk.par.T
    x = np.log(bk.k / f) - m
    r = np.sqrt(x * x + s * s)
    sig = np.sqrt(np.maximum(a + b * (rho * x + r), 0.0) / tref)
    t = np.maximum((bk.exp - mk.ts) / MS_PER_YEAR, 1e-12)
    sd = sig * np.sqrt(t)
    d1 = (np.log(f / bk.k) + 0.5 * sd * sd) / sd
    if mode == "black":
        return ndtr(d1) - bk.put
    dsig = b * (rho + x / r) / (2.0 * sig * tref)
    return ndtr(d1) - bk.put - np.exp(-0.5 * d1 * d1) / np.sqrt(2 * np.pi) * np.sqrt(t) * dsig


class MarketData:
    """Inclusive market state at any time, for any set of instruments."""

    def __init__(self, book, curve, perp: pd.DataFrame | None):
        self.book, self.curve = book, curve
        p = perp if perp is not None else pd.DataFrame(columns=["ts_ms", "mark_price_usd"])
        self._p_ts = p["ts_ms"].to_numpy("int64")
        self._p_mark = p["mark_price_usd"].to_numpy(float)
        self._basis: dict[int, tuple[np.ndarray, np.ndarray]] = {}  # exp -> (ts, basis)
        self._slice: dict[tuple[int, int], np.ndarray] = {}

    def preload_basis(self, ts_ms: np.ndarray, exp_ms: np.ndarray) -> None:
        """Every (time, expiry) combination, in one vectorised forward-curve call."""
        ts_ms, exp_ms = np.asarray(ts_ms, "int64"), np.asarray(exp_ms, "int64")
        tt, ee = (x.ravel() for x in np.meshgrid(ts_ms, exp_ms, indexing="ij"))
        self.preload_pairs(tt, ee)

    def preload_pairs(self, tt: np.ndarray, ee: np.ndarray) -> None:
        """Basis for (time, expiry) pairs, one vectorised forward-curve call; stored as a
        sorted array per expiry (a dict per pair would not fit in memory over months)."""
        tt, ee = np.asarray(tt, "int64"), np.asarray(ee, "int64")
        need_t, need_e = [], []
        for e in np.unique(ee):
            ts_e = np.unique(tt[ee == e])
            have = self._basis.get(int(e))
            if have is not None:
                ts_e = ts_e[~np.isin(ts_e, have[0])]
            need_t.append(ts_e)
            need_e.append(np.full(len(ts_e), e, "int64"))
        if not need_t or not sum(len(x) for x in need_t):
            return
        t_all, e_all = np.concatenate(need_t), np.concatenate(need_e)
        fw = self.curve.forwards(
            pd.Series(pd.to_datetime(t_all, unit="ms", utc=True)),
            pd.Series(pd.to_datetime(e_all, unit="ms", utc=True)),
            0,
            inclusive=True,
        )
        b_all = fw["basis"].to_numpy(float)
        for e in np.unique(e_all):
            sel = e_all == e
            t_new, b_new = t_all[sel], b_all[sel]
            if int(e) in self._basis:
                t_old, b_old = self._basis[int(e)]
                t_new, b_new = np.r_[t_old, t_new], np.r_[b_old, b_new]
            o = np.argsort(t_new, kind="stable")
            self._basis[int(e)] = (t_new[o], b_new[o])

    def basis(self, ts_ms: int, exp_ms: int) -> float:
        have = self._basis.get(exp_ms)
        if have is not None:
            j = int(np.searchsorted(have[0], ts_ms))
            if j < len(have[0]) and have[0][j] == ts_ms:
                return float(have[1][j])
        self.preload_pairs(np.array([ts_ms]), np.array([exp_ms]))
        return self.basis(ts_ms, exp_ms)

    def index(self, ts_ms: int) -> float:
        return self.curve.index_at(int(ts_ms), inclusive=True)

    def mark(self, ts_ms: int) -> float:
        j = np.searchsorted(self._p_ts, ts_ms, side="right") - 1
        return float(self._p_mark[j]) if j >= 0 else np.nan

    def _slice_par(self, exp: int, refit: int) -> np.ndarray:
        key = (exp, refit)
        if key not in self._slice:
            sl = self.book.slice_at_or_before(exp, refit)
            self._slice[key] = (
                np.full(6, np.nan) if sl is None else np.array([*(sl[c] for c in RAW), sl["t_ref"]])
            )
        return self._slice[key]

    def at(self, ts_ms: int, bk: Book, prev: Market | None = None) -> Market:
        ts_ms = int(ts_ms)
        s = self.index(ts_ms)
        mark = self.mark(ts_ms)
        pi = mark / s if np.isfinite(mark) else (prev.pi if prev is not None else 1.0)
        refit = int(self.book.latest_refit(np.array([ts_ms]))[0])
        n = len(bk.n)
        basis = np.empty(n)
        par = np.empty((n, 6))
        for i in range(n):
            e = int(bk.exp[i])
            basis[i] = self.basis(ts_ms, e)
            par[i] = self._slice_par(e, refit)
        if prev is not None and len(prev.basis) == n:  # carry missing inputs forward
            basis = np.where(np.isfinite(basis), basis, prev.basis)
            par = np.where(np.isfinite(par), par, prev.par)
        basis = np.where(np.isfinite(basis), basis, 1.0)  # expired: F = S
        return Market(ts_ms, s, pi, basis, par)
