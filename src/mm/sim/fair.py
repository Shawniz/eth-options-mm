"""Fair values from Surface A at each trade, using only information before t - latency.

For a trade at t on expiry e:
    cutoff  = t - latency
    R       = latest refit with R <= cutoff (a refit at R uses marks < R; tie-break order
              is refit -> quote, so a refit stamped exactly at the cutoff is usable)
    slice   = expiry e's fit AT refit R, if full or carry (no older slice, no interpolation)
    F       = strict forward, information < cutoff (D018)
    sigma   = sqrt(w(k) / t_ref), k = ln(K/F): the slice's implied vol at that moneyness,
              held over the minutes since R; priced at the trade's own time to expiry.
This does not depend on the strategy, so it is computed once, vectorised.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from mm.pricing.black76 import price_eth
from mm.pricing.forwards import ForwardCurve

LIVE = ("full", "carry")
RAW = ["a", "b", "rho", "m", "s"]


def _ms(s) -> np.ndarray:
    return pd.Series(s).astype("datetime64[ms, UTC]").astype("int64").to_numpy()


def svi_total_variance(a, b, rho, m, s, k):
    return a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + s**2))


class SurfaceBook:
    """Surface A fits indexed by (refit, expiry)."""

    def __init__(self, fits: pd.DataFrame, refit_ms: np.ndarray):
        self.grid = np.unique(np.asarray(refit_ms, dtype="int64"))
        f = fits[fits["kind"].isin(LIVE)].copy()
        f["refit_ms"] = _ms(f["refit_ts"])
        f["exp_ms"] = _ms(f["expiry"])
        self.live = f[["refit_ms", "exp_ms", "t_ref", "kind", *RAW]].reset_index(drop=True)
        self._by_exp = {
            int(e): g.sort_values("refit_ms").reset_index(drop=True)
            for e, g in self.live.groupby("exp_ms")
        }

    def latest_refit(self, cutoff_ms) -> np.ndarray:
        i = np.searchsorted(self.grid, np.asarray(cutoff_ms, dtype="int64"), side="right") - 1
        return np.where(i >= 0, self.grid[np.clip(i, 0, None)], -1)

    def slice_at_or_before(self, exp_ms: int, refit_ms: int):
        """Latest live slice for this expiry at or before refit_ms (valuation only)."""
        g = self._by_exp.get(int(exp_ms))
        if g is None:
            return None
        i = int(np.searchsorted(g["refit_ms"].to_numpy(), refit_ms, side="right")) - 1
        return None if i < 0 else g.iloc[i]


def fair_values(
    trades: pd.DataFrame, book: SurfaceBook, curve: ForwardCurve, latency_ms: int
) -> pd.DataFrame:
    """trades: ts, expiry, strike_usd, option_type, t_years. Adds refit_ms, fit_kind,
    forward_q, index_q, sigma_fair, fair_eth (NaN where there is no surface/forward)."""
    out = trades.reset_index(drop=True).copy()
    ts_ms = _ms(out["ts"])
    out["ts_ms"] = ts_ms
    out["exp_ms"] = _ms(out["expiry"])
    out["refit_ms"] = book.latest_refit(ts_ms - int(latency_ms))
    fw = curve.forwards(out["ts"], out["expiry"], latency_ms)  # strict: < t - latency
    out["forward_q"] = fw["forward_usd"].to_numpy()
    out["index_q"] = fw["index_usd"].to_numpy()
    j = out[["refit_ms", "exp_ms"]].merge(
        book.live, on=["refit_ms", "exp_ms"], how="left", validate="many_to_one"
    )
    out["fit_kind"] = j["kind"].to_numpy()
    out["t_ref"] = j["t_ref"].to_numpy()
    k = np.log(out["strike_usd"].to_numpy() / out["forward_q"].to_numpy())
    w = svi_total_variance(*(j[c].to_numpy() for c in RAW), k)
    with np.errstate(invalid="ignore"):
        out["sigma_fair"] = np.sqrt(np.maximum(w, 0.0) / out["t_ref"].to_numpy())
    call = (out["option_type"] == "C").to_numpy()
    out["fair_eth"] = price_eth(
        out["forward_q"].to_numpy(),
        out["strike_usd"].to_numpy(),
        out["t_years"].to_numpy(),
        out["sigma_fair"].to_numpy(),
        call,
    )
    return out
