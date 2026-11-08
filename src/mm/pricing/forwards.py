"""Forward per expiry at time t:  F(t) = basis(t) x index(t).

The basis (F / index) is carry and moves slowly; the index moves every second. Each comes
from its freshest source, so a basis that is minutes old is fine while the index is never
more than about a second old (the perp trades constantly and every trade carries the
index). See decisions D017–D018.

Basis sources, in order of preference:
    future  the matching dated future: mark / index from the SAME trade
    parity  put-call parity on option marks, F = K / (1 - (c - p)), median of recent
            near-ATM call/put pairs (a pair is known once both legs have traded)
    interp  ln(basis) interpolated linearly in T between expiries that have a basis
            (ln basis = 0 at T = 0; flat annualised carry beyond the last expiry)
    none    no index yet, expired, or nothing to interpolate from

No lookahead: every input is strictly before cutoff = t - latency.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from mm.config import ForwardsConfig
from mm.pricing.time import MS_PER_YEAR

SOURCES = ("future", "parity", "interp", "none")


def _ms(s: pd.Series) -> np.ndarray:
    return s.astype("datetime64[ms, UTC]").astype("int64").to_numpy()


def future_basis_obs(future_trades: pd.DataFrame) -> pd.DataFrame:
    """One basis observation per dated-future trade: mark / index from that trade."""
    df = future_trades.dropna(subset=["expiry", "mark_price_usd", "index_price_usd"])
    out = pd.DataFrame(
        {
            "ts": df["ts"],
            "expiry": df["expiry"],
            "basis": df["mark_price_usd"] / df["index_price_usd"],
        }
    )
    return out.sort_values("ts", kind="stable").reset_index(drop=True)


def parity_basis_obs(option_trades: pd.DataFrame, cfg: ForwardsConfig) -> pd.DataFrame:
    """Basis observations from call/put pairs at the same expiry and strike whose trades
    are at most parity_pair_window_s apart. Uses Deribit's mark prices (attached to each
    trade), which embed Deribit's own forward. Pair time = the later leg."""
    cols = ["ts", "expiry", "strike_usd", "mark_price_eth", "index_price_usd"]
    df = option_trades.sort_values("ts", kind="stable")
    calls = df.loc[df["option_type"] == "C", cols]
    puts = df.loc[df["option_type"] == "P", cols].assign(ts_p=lambda x: x["ts"])
    empty = pd.DataFrame({"ts": pd.Series(dtype="datetime64[ms, UTC]"), "expiry": [], "basis": []})
    if calls.empty or puts.empty:
        return empty
    m = pd.merge_asof(
        calls,
        puts,
        on="ts",
        by=["expiry", "strike_usd"],
        direction="nearest",
        tolerance=pd.Timedelta(seconds=cfg.parity_pair_window_s),
        suffixes=("_c", "_p"),
    ).dropna(subset=["ts_p"])
    if m.empty:
        return empty
    k = m["strike_usd"]
    idx = 0.5 * (m["index_price_usd_c"] + m["index_price_usd_p"])
    denom = 1.0 - (m["mark_price_eth_c"] - m["mark_price_eth_p"])
    with np.errstate(divide="ignore", invalid="ignore"):
        basis = (k / denom) / idx
    lo, hi = cfg.basis_bounds
    keep = (
        (denom > 0) & (np.abs(np.log(k / idx)) <= cfg.parity_atm_band) & (basis > lo) & (basis < hi)
    )
    m, basis = m[keep], basis[keep]
    out = pd.DataFrame(
        {
            "ts": np.maximum(m["ts"], m["ts_p"]).astype("datetime64[ms, UTC]"),
            "expiry": m["expiry"],
            "basis": basis.astype(float),
        }
    )
    return out.sort_values("ts", kind="stable").reset_index(drop=True)


@dataclass(frozen=True)
class ForwardResult:
    forward_usd: float
    basis: float
    source: str
    index_usd: float
    basis_age_s: float


class ForwardCurve:
    def __init__(
        self,
        index: pd.DataFrame,
        future_obs: pd.DataFrame,
        parity_obs: pd.DataFrame,
        cfg: ForwardsConfig,
    ):
        self.cfg = cfg
        idx = index.dropna(subset=["index_price_usd"]).sort_values("ts", kind="stable")
        self._idx_ts = _ms(idx["ts"])
        self._idx_val = idx["index_price_usd"].to_numpy(float)

        lo, hi = cfg.basis_bounds
        self._fut: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        f = future_obs[(future_obs["basis"] > lo) & (future_obs["basis"] < hi)]
        for exp, g in f.groupby("expiry"):
            g = g.sort_values("ts", kind="stable")
            self._fut[int(pd.Timestamp(exp).value // 10**6)] = (
                _ms(g["ts"]),
                g["basis"].to_numpy(float),
            )

        self._par: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        for exp, g in parity_obs.groupby("expiry"):
            g = g.sort_values("ts", kind="stable")
            med = (
                g["basis"]
                .rolling(cfg.parity_n_pairs, min_periods=cfg.parity_min_pairs)
                .median()
                .to_numpy(float)
            )
            self._par[int(pd.Timestamp(exp).value // 10**6)] = (_ms(g["ts"]), med)

        self._known = np.array(sorted(set(self._fut) | set(self._par)), dtype="int64")

    # --- basis for one expiry at many cutoffs ---
    @staticmethod
    def _latest(ts: np.ndarray, val: np.ndarray, cutoff: np.ndarray, max_age_ms: float):
        i = np.searchsorted(ts, cutoff, side="left") - 1  # last obs strictly before cutoff
        ok = i >= 0
        b = np.full(cutoff.shape, np.nan)
        age = np.full(cutoff.shape, np.nan)
        b[ok] = val[i[ok]]
        age[ok] = (cutoff[ok] - ts[i[ok]]) / 1000.0
        stale = ok & (age * 1000.0 > max_age_ms)
        b[stale] = np.nan
        return b, age

    def _basis_direct(self, exp_ms: int, cutoff: np.ndarray):
        basis = np.full(cutoff.shape, np.nan)
        age = np.full(cutoff.shape, np.nan)
        src = np.full(cutoff.shape, "none", dtype=object)
        if exp_ms in self._fut:
            ts, val = self._fut[exp_ms]
            b, a = self._latest(ts, val, cutoff, self.cfg.future_basis_max_age_s * 1000)
            hit = np.isfinite(b)
            basis[hit], age[hit], src[hit] = b[hit], a[hit], "future"
        if exp_ms in self._par:
            ts, val = self._par[exp_ms]
            b, a = self._latest(ts, val, cutoff, self.cfg.parity_max_age_s * 1000)
            hit = np.isfinite(b) & ~np.isfinite(basis)
            basis[hit], age[hit], src[hit] = b[hit], a[hit], "parity"
        return basis, age, src

    def _interp(self, exp_ms: np.ndarray, cutoff: np.ndarray) -> np.ndarray:
        """ln(basis) linear in T through (0, 0) and every expiry with a direct basis."""
        out = np.full(cutoff.shape, np.nan)
        if len(self._known) == 0 or len(cutoff) == 0:
            return out
        grid = np.full((len(cutoff), len(self._known)), np.nan)
        for j, e in enumerate(self._known):
            b, _, _ = self._basis_direct(int(e), cutoff)
            grid[:, j] = np.log(b)
        t_known = (self._known[None, :] - cutoff[:, None]) / MS_PER_YEAR
        t_q = (exp_ms - cutoff) / MS_PER_YEAR
        for r in range(len(cutoff)):
            ok = np.isfinite(grid[r]) & (t_known[r] > 0)
            if not ok.any() or t_q[r] <= 0:
                continue
            ts_, lb = t_known[r][ok], grid[r][ok]
            order = np.argsort(ts_)
            ts_, lb = np.r_[0.0, ts_[order]], np.r_[0.0, lb[order]]
            if t_q[r] <= ts_[-1]:
                out[r] = np.exp(np.interp(t_q[r], ts_, lb))
            else:  # flat annualised carry beyond the last expiry
                out[r] = np.exp(lb[-1] / ts_[-1] * t_q[r])
        return out

    def forwards(self, ts: pd.Series, expiry: pd.Series, latency_ms: int) -> pd.DataFrame:
        cutoff = _ms(pd.Series(ts).reset_index(drop=True)) - int(latency_ms)
        exp_ms = _ms(pd.Series(expiry).reset_index(drop=True))
        n = len(cutoff)
        j = np.searchsorted(self._idx_ts, cutoff, side="left") - 1
        index = np.where(j >= 0, self._idx_val[np.clip(j, 0, None)], np.nan) if n else np.array([])

        basis = np.full(n, np.nan)
        age = np.full(n, np.nan)
        src = np.full(n, "none", dtype=object)
        for e in np.unique(exp_ms):
            rows = np.flatnonzero(exp_ms == e)
            b, a, s = self._basis_direct(int(e), cutoff[rows])
            basis[rows], age[rows], src[rows] = b, a, s

        need = ~np.isfinite(basis) & (exp_ms > cutoff)
        if need.any():
            rows = np.flatnonzero(need)
            b = self._interp(exp_ms[rows], cutoff[rows])
            hit = np.isfinite(b)
            basis[rows[hit]] = b[hit]
            src[rows[hit]] = "interp"

        expired = exp_ms <= cutoff
        basis[expired] = np.nan
        src[expired | ~np.isfinite(index)] = "none"
        fwd = np.where(src != "none", basis * index, np.nan)
        return pd.DataFrame(
            {
                "forward_usd": fwd,
                "basis": basis,
                "source": pd.Categorical(src, categories=SOURCES),
                "index_usd": index,
                "basis_age_s": age,
            }
        )

    def forward(self, expiry, at, latency_ms: int) -> ForwardResult:
        r = self.forwards(
            pd.Series([pd.Timestamp(at)]), pd.Series([pd.Timestamp(expiry)]), latency_ms
        )
        row = r.iloc[0]
        return ForwardResult(
            float(row["forward_usd"]),
            float(row["basis"]),
            str(row["source"]),
            float(row["index_usd"]),
            float(row["basis_age_s"]),
        )
