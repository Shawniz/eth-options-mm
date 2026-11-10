"""Experiment metrics: daily P&L, Sharpe, drawdown, paired differences, regimes, selection."""

from __future__ import annotations

import numpy as np
import pandas as pd

from mm.pnl.attribution import COMPONENTS

DAYS_PER_YEAR = 365.0


def daily_components(rows: pd.DataFrame) -> pd.DataFrame:
    """Attribution rows -> one row per UTC day: components and total (USD)."""
    t = pd.to_datetime(rows["ts_ms"], unit="ms", utc=True)
    # a value change stamped exactly at midnight belongs to the day that just ended
    day = (t - pd.Timedelta(milliseconds=1)).dt.floor("D").dt.date
    d = rows[COMPONENTS].groupby(day.to_numpy()).sum()
    d["total"] = d.sum(axis=1)
    d.index.name = "day"
    return d


def sharpe(pnl: pd.Series) -> float:
    sd = pnl.std(ddof=1)
    return float(pnl.mean() / sd * np.sqrt(DAYS_PER_YEAR)) if sd > 0 else float("nan")


def max_drawdown(pnl: pd.Series) -> float:
    """Most negative fall of cumulative P&L from its running peak (starting at 0)."""
    cum = np.r_[0.0, pnl.cumsum().to_numpy()]
    return float((cum - np.maximum.accumulate(cum)).min())


def paired_diff(var: pd.Series, base: pd.Series) -> dict:
    """Daily (variant - baseline) on common days: mean, 2 x standard error, n, lag-1 AC."""
    d = (var - base).dropna()
    n = len(d)
    if n < 2:
        return {"mean": float("nan"), "two_se": float("nan"), "n": n, "ac1": float("nan")}
    ac1 = float(d.autocorr(1)) if n > 2 and d.std() > 0 else float("nan")
    return {"mean": float(d.mean()), "two_se": float(2 * d.std(ddof=1) / np.sqrt(n)),
            "n": n, "ac1": ac1}  # fmt: skip


def stress_threshold(dvol_daily: pd.Series, train_start, train_end, q: float) -> float:
    """Quantile of daily mean DVOL over TRAIN days only [train_start, train_end)."""
    idx = pd.Index(dvol_daily.index)
    train = dvol_daily[(idx >= train_start) & (idx < train_end)]
    return float(np.quantile(train.dropna().to_numpy(), q))


def choose_best(scores: dict[str, float]) -> str:
    """Highest score (mean daily P&L on the training period); ties -> first listed."""
    return max(scores, key=lambda k: (scores[k], -list(scores).index(k)))
