"""Forwards: F(t) = basis x index, every input strictly before t - latency."""

import numpy as np
import pandas as pd
import pytest

from mm.pricing.black76 import price_eth
from mm.pricing.forwards import (
    ForwardCurve,
    future_basis_obs,
    parity_basis_obs,
)

T0 = pd.Timestamp("2025-03-03 12:00", tz="UTC")
MAR28 = pd.Timestamp("2025-03-28 08:00", tz="UTC")  # has a dated future
MAR07 = pd.Timestamp("2025-03-07 08:00", tz="UTC")  # weekly, no future
MAR04 = pd.Timestamp("2025-03-04 08:00", tz="UTC")  # daily, no future
MIN = pd.Timedelta("1min")


@pytest.fixture
def fcfg(cfg):
    return cfg.pricing.forwards


def _fut(rows):
    """rows: (ts, mark, index) for ETH-28MAR25."""
    return pd.DataFrame(
        [
            {
                "ts": ts,
                "instrument_name": "ETH-28MAR25",
                "expiry": MAR28,
                "mark_price_usd": m,
                "index_price_usd": i,
            }
            for ts, m, i in rows
        ]
    )


def _idx(rows):
    return pd.DataFrame([{"ts": ts, "index_price_usd": i} for ts, i in rows])


def _empty_parity():
    return pd.DataFrame(columns=["ts", "expiry", "basis"])


def _curve(fut, idx, fcfg, parity=None):
    return ForwardCurve(
        idx, future_basis_obs(fut), parity if parity is not None else _empty_parity(), fcfg
    )


# ---------- futures ----------


def test_future_basis_is_mark_over_index_from_same_trade():
    obs = future_basis_obs(_fut([(T0, 2020.0, 2000.0)]))
    assert obs["basis"].iloc[0] == pytest.approx(1.01)
    assert obs["expiry"].iloc[0] == MAR28


def test_future_expiry_uses_basis_times_latest_index(fcfg):
    fc = _curve(_fut([(T0, 2020.0, 2000.0)]), _idx([(T0, 2000.0), (T0 + 20 * MIN, 2100.0)]), fcfg)
    r = fc.forward(MAR28, at=T0 + 21 * MIN, latency_ms=0)
    assert r.forward_usd == pytest.approx(1.01 * 2100.0)  # not the stale 2020
    assert r.source == "future"


def test_no_lookahead_on_futures(fcfg):
    t = T0 + 10 * MIN
    fut = _fut([(T0, 2020.0, 2000.0), (t, 2500.0, 2000.0)])  # a print exactly AT t
    fc = _curve(fut, _idx([(T0, 2000.0), (t, 9999.0)]), fcfg)
    r = fc.forward(MAR28, at=t, latency_ms=0)
    assert r.forward_usd == pytest.approx(2020.0)  # inputs strictly before t


def test_latency_shifts_the_cutoff(fcfg):
    t = T0 + 10 * MIN
    idx = _idx([(T0, 2000.0), (t - pd.Timedelta("500ms"), 2200.0)])
    fc = _curve(_fut([(T0, 2020.0, 2000.0)]), idx, fcfg)
    assert fc.forward(MAR28, at=t, latency_ms=0).forward_usd == pytest.approx(1.01 * 2200.0)
    assert fc.forward(MAR28, at=t, latency_ms=1000).forward_usd == pytest.approx(1.01 * 2000.0)


def test_stale_future_basis_is_not_used(fcfg):
    old = T0 - pd.Timedelta(seconds=fcfg.future_basis_max_age_s + 60)
    fc = _curve(_fut([(old, 2020.0, 2000.0)]), _idx([(T0, 2000.0)]), fcfg)
    assert fc.forward(MAR28, at=T0 + MIN, latency_ms=0).source != "future"


# ---------- put-call parity ----------


def _opt(ts, expiry, k, cp, mark_eth, index):
    return {
        "ts": ts,
        "expiry": expiry,
        "strike_usd": k,
        "option_type": cp,
        "mark_price_eth": mark_eth,
        "index_price_usd": index,
    }


def _pair_trades(f_true, index, expiry, t_years, ts, strikes=(1950.0, 2000.0, 2050.0), dt_s=5):
    rows = []
    for j, k in enumerate(strikes):
        c = price_eth(f_true, k, t_years, 0.7, True)
        p = price_eth(f_true, k, t_years, 0.7, False)
        t_c = ts + pd.Timedelta(seconds=10 * j)
        rows.append(_opt(t_c, expiry, k, "C", c, index))
        rows.append(_opt(t_c + pd.Timedelta(seconds=dt_s), expiry, k, "P", p, index))
    return pd.DataFrame(rows)


def test_parity_recovers_forward_from_marks(fcfg):
    trades = _pair_trades(2004.0, 2000.0, MAR07, 4 / 365, T0)
    obs = parity_basis_obs(trades, fcfg)
    assert len(obs) == 3
    assert obs["basis"].to_numpy() == pytest.approx(np.full(3, 1.002))
    # a pair is known only once BOTH legs have traded
    assert obs["ts"].min() == T0 + pd.Timedelta(seconds=5)


def test_parity_ignores_pairs_too_far_apart_or_off_atm(fcfg):
    far = _pair_trades(2004.0, 2000.0, MAR07, 4 / 365, T0, dt_s=fcfg.parity_pair_window_s + 1)
    wing = _pair_trades(2004.0, 2000.0, MAR07, 4 / 365, T0, strikes=(1500.0, 2600.0))
    assert len(parity_basis_obs(far, fcfg)) == 0
    assert len(parity_basis_obs(wing, fcfg)) == 0


def test_parity_forward_for_expiry_without_future(fcfg):
    parity = parity_basis_obs(_pair_trades(2004.0, 2000.0, MAR07, 4 / 365, T0), fcfg)
    fc = _curve(
        _fut([(T0, 2020.0, 2000.0)]), _idx([(T0, 2000.0), (T0 + MIN, 2010.0)]), fcfg, parity
    )
    r = fc.forward(MAR07, at=T0 + 2 * MIN, latency_ms=0)
    assert r.source == "parity"
    assert r.forward_usd == pytest.approx(1.002 * 2010.0)


def test_parity_needs_min_pairs(fcfg):
    trades = _pair_trades(2004.0, 2000.0, MAR07, 4 / 365, T0, strikes=(2000.0,))
    parity = parity_basis_obs(trades, fcfg)
    assert len(parity) < fcfg.parity_min_pairs
    fc = _curve(_fut([(T0, 2020.0, 2000.0)]), _idx([(T0, 2000.0)]), fcfg, parity)
    assert fc.forward(MAR07, at=T0 + 2 * MIN, latency_ms=0).source == "interp"


def test_parity_agrees_with_future_where_both_exist(fcfg):
    # Marks generated from the future's forward must give back the future's basis.
    t_years = (MAR28 - T0).total_seconds() / (365 * 86400)
    parity = parity_basis_obs(_pair_trades(2020.0, 2000.0, MAR28, t_years, T0), fcfg)
    assert parity["basis"].median() == pytest.approx(
        future_basis_obs(_fut([(T0, 2020.0, 2000.0)]))["basis"].iloc[0], rel=1e-9
    )


# ---------- fallback ----------


def test_fallback_interpolates_log_basis_in_T_and_is_flagged(fcfg):
    # Only the MAR28 future exists. MAR04 (no future, no pairs) is interpolated between
    # ln b = 0 at T = 0 and the MAR28 basis.
    at = T0 + MIN
    fc = _curve(_fut([(T0, 2020.0, 2000.0)]), _idx([(T0, 2000.0)]), fcfg)
    r = fc.forward(MAR04, at=at, latency_ms=0)
    assert r.source == "interp"
    t4 = (MAR04 - at).total_seconds()
    t28 = (MAR28 - at).total_seconds()
    expected = 2000.0 * np.exp(np.log(1.01) * t4 / t28)
    assert r.forward_usd == pytest.approx(expected)


def test_no_index_gives_nan(fcfg):
    fc = _curve(_fut([(T0, 2020.0, 2000.0)]), _idx([(T0 + 5 * MIN, 2000.0)]), fcfg)
    r = fc.forward(MAR28, at=T0 + MIN, latency_ms=0)
    assert np.isnan(r.forward_usd) and r.source == "none"


# ---------- vectorised path used on 1.7M trades ----------


def test_vectorised_matches_scalar(fcfg):
    parity = parity_basis_obs(_pair_trades(2004.0, 2000.0, MAR07, 4 / 365, T0), fcfg)
    fc = _curve(
        _fut([(T0, 2020.0, 2000.0)]), _idx([(T0, 2000.0), (T0 + MIN, 2010.0)]), fcfg, parity
    )
    ts = pd.Series([T0 + 2 * MIN, T0 + 2 * MIN, T0 + 3 * MIN])
    exp = pd.Series([MAR07, MAR28, MAR04])
    out = fc.forwards(ts, exp, latency_ms=0)
    for i in range(3):
        one = fc.forward(exp[i], at=ts[i], latency_ms=0)
        assert out["forward_usd"].iloc[i] == pytest.approx(one.forward_usd)
        assert out["source"].iloc[i] == one.source
