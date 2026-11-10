"""Phase 8: overrides, metrics, train-only thresholds and selection, determinism."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from test_sim import _inputs, _market, _perp, _trades

from mm.experiments.metrics import (
    choose_best,
    daily_components,
    max_drawdown,
    paired_diff,
    sharpe,
    stress_threshold,
)
from mm.experiments.runner import apply_override, plan
from mm.pnl.attribution import attribute
from mm.pnl.value import MarketData
from mm.sim.engine import run

DAYS = [dt.date(2025, 5, d) for d in range(1, 6)]


def test_variant_override_is_validated(cfg):
    v = apply_override(cfg, {"quoting": {"half_spread_volpts": 0.5}})
    assert v.quoting.half_spread_volpts == 0.5
    assert v.quoting.kappa_volpts == cfg.quoting.kappa_volpts  # the rest untouched
    with pytest.raises(ValidationError):
        apply_override(cfg, {"quoting": {"half_sprad_volpts": 0.5}})  # typo fails loudly


def test_plan_has_one_baseline_and_one_run_per_other_value(cfg):
    exp = {"experiments": {
        "spread": {"param": ["quoting", "half_spread_volpts"], "values": [0.5, 1.0, 2.0]},
        "band": {"param": ["hedging", "delta_band_eth"], "values": [5.0, 10.0]},
    }}  # fmt: skip
    p = plan(cfg, exp)
    assert set(p) == {"baseline", "spread=0.5", "spread=2", "band=10"}
    assert p["band=10"] == {"hedging": {"delta_band_eth": 10.0}}
    bad = {"experiments": {"x": {"param": ["hedging", "delta_band_eth"], "values": [1.0]}}}
    with pytest.raises(ValueError):
        plan(cfg, bad)  # the base value must be one of the values


def test_paired_difference_mean_and_two_se():
    base = pd.Series([10.0, -5.0, 3.0, 8.0], index=DAYS[:4])
    var = base + pd.Series([1.0, 2.0, 3.0, 2.0], index=DAYS[:4])
    r = paired_diff(var, base)
    assert r["mean"] == pytest.approx(2.0) and r["n"] == 4
    assert r["two_se"] == pytest.approx(2 * np.std([1, 2, 3, 2], ddof=1) / 2.0)


def test_paired_difference_uses_only_common_days():
    r = paired_diff(pd.Series([1.0, 2.0], index=DAYS[:2]),
                    pd.Series([0.0, 0.0, 9.0], index=DAYS[:3]))  # fmt: skip
    assert r["n"] == 2 and r["mean"] == pytest.approx(1.5)


def test_sharpe_and_drawdown():
    pnl = pd.Series([100.0, -50.0, 100.0, -200.0, 50.0])
    assert sharpe(pnl) == pytest.approx(pnl.mean() / pnl.std(ddof=1) * np.sqrt(365))
    assert max_drawdown(pnl) == pytest.approx(-200.0)  # peak 150 -> trough -50
    assert max_drawdown(pd.Series([-30.0, 10.0])) == pytest.approx(-30.0)  # from 0


def test_stress_threshold_comes_from_training_only():
    train = [dt.date(2025, 1, d) for d in range(1, 6)]
    test = [dt.date(2025, 6, d) for d in range(1, 4)]
    dvol = pd.Series([50, 60, 70, 80, 90, 55, 200, 300], index=train + test, dtype=float)
    thr = stress_threshold(dvol, dt.date(2025, 1, 1), dt.date(2025, 2, 1), 0.8)
    assert thr == pytest.approx(np.quantile([50, 60, 70, 80, 90], 0.8))
    dvol2 = dvol.copy()
    dvol2[test] = 1000.0  # changing test-period DVOL ...
    assert stress_threshold(dvol2, dt.date(2025, 1, 1), dt.date(2025, 2, 1), 0.8) == thr


def test_best_is_chosen_from_the_scores_given():
    train = {"baseline": 5.0, "spread=2": 9.0, "spread=3": 9.0}
    assert choose_best(train) == "spread=2"  # highest; ties -> first listed


def test_midnight_change_belongs_to_the_day_that_ended():
    rows = pd.DataFrame({"ts_ms": [int(pd.Timestamp("2025-01-07", tz="UTC").value // 10**6)],
                         "theta": [5.0]})  # fmt: skip
    for c in (
        "delta",
        "gamma",
        "basis",
        "vega",
        "spread_capture",
        "hedge_cost",
        "option_fee",
        "perp_fee",
        "delivery_fee",
        "funding",
        "settlement",
    ):
        rows[c] = 0.0
    d = daily_components(rows)
    assert list(d.index) == [dt.date(2025, 1, 6)] and d["total"].iloc[0] == 5.0


def test_runs_are_deterministic(cfg):
    def once():
        book, curve = _market(cfg)
        sells = [(f"2025-01-06 12:0{m}:30", "buy", 0.5, 100) for m in range(4)]
        t0 = pd.Timestamp("2025-01-06 11:59", tz="UTC")
        rows = [(t0 + pd.Timedelta(seconds=5 * i), "buy" if i % 2 else "sell",
                 3000.0 - 0.1 * i) for i in range(2000)]  # fmt: skip
        inp = _inputs(cfg, _trades(sells), book, curve, perp=_perp(rows))
        res = run(inp, cfg)
        att = attribute(res.fills, res.hedges, res.ledger, res.daily,
                        MarketData(inp.book, inp.curve, inp.perp), cfg.sim.initial_eth,
                        inp.start_ms, inp.end_ms)  # fmt: skip
        return daily_components(att.rows)

    pd.testing.assert_frame_equal(once(), once())
