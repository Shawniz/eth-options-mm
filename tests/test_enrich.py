import pandas as pd
import pytest

from mm.pricing.black76 import price_eth
from mm.pricing.enrich import index_series, price_trades
from mm.pricing.forwards import ForwardCurve, future_basis_obs

T0 = pd.Timestamp("2025-03-03 12:00", tz="UTC")
MAR28 = pd.Timestamp("2025-03-28 08:00", tz="UTC")


def test_index_series_merges_all_streams():
    a = pd.DataFrame({"ts": [T0, T0 + pd.Timedelta("2s")], "index_price_usd": [1.0, 3.0]})
    b = pd.DataFrame({"ts": [T0 + pd.Timedelta("1s")], "index_price_usd": [2.0]})
    s = index_series([a, b])
    assert s["index_price_usd"].tolist() == [1.0, 2.0, 3.0]
    assert s["ts"].is_monotonic_increasing


def test_price_trades_inclusive_and_strict(cfg):
    fcfg = cfg.pricing.forwards
    fut = pd.DataFrame(
        [
            {
                "ts": T0,
                "instrument_name": "ETH-28MAR25",
                "expiry": MAR28,
                "mark_price_usd": 2020.0,
                "index_price_usd": 2000.0,
            }
        ]
    )
    idx = pd.DataFrame({"ts": [T0], "index_price_usd": [2000.0]})
    curve = ForwardCurve(
        idx, future_basis_obs(fut), pd.DataFrame(columns=["ts", "expiry", "basis"]), fcfg
    )
    t = T0 + pd.Timedelta("1min")
    t_years = (MAR28 - t).total_seconds() / (365 * 86400)
    f_true = 1.01 * 2010.0  # the trade's own index is 2010
    px = price_eth(f_true, 2000.0, t_years, 0.7, True)
    trades = pd.DataFrame(
        [
            {
                "ts": t,
                "expiry": MAR28,
                "strike_usd": 2000.0,
                "option_type": "C",
                "t_years": t_years,
                "price_eth": px,
                "mark_price_eth": px,
                "index_price_usd": 2010.0,
            }
        ]
    )
    out = price_trades(trades, curve, latency_ms=0)
    assert out["forward_usd"].iloc[0] == pytest.approx(f_true)  # <= t, own index
    assert out["trade_iv_ours"].iloc[0] == pytest.approx(0.7, abs=1e-9)
    assert out["mark_iv"].iloc[0] == pytest.approx(0.7, abs=1e-9)
    assert out["forward_strict_usd"].iloc[0] == pytest.approx(1.01 * 2000.0)  # < t
    assert out["trade_iv_strict"].iloc[0] != pytest.approx(0.7, abs=1e-6)
