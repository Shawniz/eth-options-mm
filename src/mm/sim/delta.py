"""Option-book delta, fast enough to evaluate at every perpetual print.

delta_options = sum n_i x Black delta_i, with F_i = basis_i x S and sigma_i from the
expiry's latest live slice at the current moneyness k = ln(K/F) (sticky moneyness between
refits, as for quotes, D029). Per-position inputs (basis, slice) are rebuilt when the
book changes or a new refit becomes available; between rebuilds only S and T move.
Only information before t is used: the refit is the latest at or before t (it uses marks
before its stamp) and the basis comes from forwards strictly before t.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.special import ndtr

from mm.pricing.time import MS_PER_YEAR

RAW = ("a", "b", "rho", "m", "s")


class OptionDelta:
    def __init__(self, book, curve, meta):
        self.book, self.curve, self.meta = book, curve, meta
        self.dirty = True
        self.refit = None
        self.n_missing = 0  # positions with no slice/forward at the last rebuild
        self._n = np.zeros(0)

    def invalidate(self) -> None:
        self.dirty = True

    def _rebuild(self, acct, ts_ms: int, refit_ms: int) -> None:
        names = list(acct.positions)
        self.refit, self.dirty = refit_ms, False
        if not names:
            self._n = np.zeros(0)
            self.n_missing = 0
            return
        m = [self.meta(x) for x in names]
        exp = np.array([x[0] for x in m], "int64")
        ts = pd.Series(pd.to_datetime(np.full(len(names), ts_ms), unit="ms", utc=True))
        fw = self.curve.forwards(ts, pd.Series(pd.to_datetime(exp, unit="ms", utc=True)), 0)
        basis = (fw["forward_usd"] / fw["index_usd"]).to_numpy()
        par = np.full((len(names), 6), np.nan)
        for i, e in enumerate(exp):
            sl = self.book.slice_at_or_before(int(e), refit_ms)
            if sl is not None:
                par[i] = [sl[c] for c in RAW] + [sl["t_ref"]]
        ok = np.isfinite(basis) & np.isfinite(par).all(axis=1)
        self.n_missing = int((~ok).sum())
        self._n = np.array([acct.positions[x] for x in names])[ok]
        self._exp = exp[ok]
        self._k = np.array([x[1] for x in m])[ok]
        self._put = ~np.array([x[2] for x in m])[ok]
        self._basis = basis[ok]
        self._a, self._b, self._rho, self._m, self._s, self._tref = par[ok].T

    def total(self, acct, ts_ms: int, s_usd: float) -> float:
        r = int(self.book.latest_refit(np.array([ts_ms]))[0])
        if self.dirty or r != self.refit:
            self._rebuild(acct, ts_ms, r)
        if not len(self._n):
            return 0.0
        f = self._basis * s_usd
        lk = np.log(self._k / f)
        x = lk - self._m
        w = self._a + self._b * (self._rho * x + np.sqrt(x * x + self._s * self._s))
        sig = np.sqrt(np.maximum(w, 0.0) / self._tref)
        t = (self._exp - ts_ms) / MS_PER_YEAR
        sd = sig * np.sqrt(np.maximum(t, 1e-12))
        d = ndtr((-lk + 0.5 * sd * sd) / sd) - self._put
        return float(np.dot(self._n, d))


def option_value_eth(od: OptionDelta, acct, ts_ms: int, s_usd: float) -> float:
    """sum n_i C_i/F_i (ETH) on the same inputs as OptionDelta.total (bump checks)."""
    from mm.pricing.black76 import price_eth

    od.total(acct, ts_ms, s_usd)  # make sure the inputs are current
    if not len(od._n):
        return 0.0
    f = od._basis * s_usd
    x = np.log(od._k / f) - od._m
    w = od._a + od._b * (od._rho * x + np.sqrt(x * x + od._s * od._s))
    sig = np.sqrt(np.maximum(w, 0.0) / od._tref)
    t = (od._exp - ts_ms) / MS_PER_YEAR
    return float(np.dot(od._n, price_eth(f, od._k, t, sig, ~od._put)))
