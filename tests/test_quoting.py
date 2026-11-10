"""Phase 5 quoter: each parameter moves quotes the right way; properties hold for any input."""

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mm.config import load_config
from mm.pricing.black76 import delta, price_eth, vega
from mm.quoting.quoter import (
    in_universe,
    make_quote,
    otm_delta,
    quote_size,
    round_ask,
    round_bid,
    skew_volpts,
)
from mm.risk.greeks import book_vega_usd
from mm.sim.fees import option_fee_eth

F, VOL, T30 = 3300.0, 0.70, 30 / 365
CFG = load_config(__import__("pathlib").Path(__file__).parents[1] / "config" / "base.yaml")


def q(cfg, k=3300.0, t=T30, call=True, nv=0.0, sigma=VOL, vref=None, **upd):
    qc = cfg.quoting.model_copy(update=upd)
    vref = cfg.risk.vega_limit_usd if vref is None else vref
    return make_quote(sigma, F, k, t, call, nv, qc, cfg.specs.option, vref)


def on_grid(p, spec):
    tick = spec.tick_for(p - 1e-12)
    return abs(p / tick - round(p / tick)) < 1e-6


# ---------------- rounding ----------------


def test_rounding_bid_down_ask_up_with_price_dependent_tick(cfg):
    opt = cfg.specs.option
    assert round_bid(0.00347, opt) == pytest.approx(0.0034)
    assert round_ask(0.00341, opt) == pytest.approx(0.0035)
    assert round_bid(0.0123, opt) == pytest.approx(0.0120)  # 0.0005 tick above 0.005
    assert round_ask(0.0121, opt) == pytest.approx(0.0125)
    assert round_bid(0.0120, opt) == pytest.approx(0.0120)  # already on the grid


def test_bid_rounds_down_ask_rounds_up(cfg):
    r, spec = q(cfg, half_spread_volpts=1.3), cfg.specs.option
    lo, hi = (price_eth(F, 3300.0, T30, VOL + s * 0.013, True) for s in (-1, 1))
    assert r.bid <= lo and r.ask >= hi
    assert lo - r.bid < spec.tick_for(lo) and r.ask - hi < spec.tick_for(hi)
    assert on_grid(r.bid, spec) and on_grid(r.ask, spec)


# ---------------- spread and floor ----------------


def test_wider_half_spread_widens_both_sides(cfg):
    a, b = q(cfg, half_spread_volpts=1.0), q(cfg, half_spread_volpts=3.0)
    assert b.bid < a.bid and b.ask > a.ask


def test_fee_floor_binds_on_cheap_option(cfg):
    k, t = 4000.0, 7 / 365
    r = q(cfg, k=k, t=t, half_spread_volpts=0.1)
    fair = price_eth(F, k, t, VOL, True)
    fee = option_fee_eth(1.0, fair, cfg.specs.option, maker=True)
    assert r.floor_bound
    assert r.ask - fair >= fee - 1e-12 and fair - r.bid >= fee - 1e-12


def test_higher_fee_cover_widens(cfg):
    # cheap option (0.0001 tick) so the fee, not the tick, sets the floor
    kw = dict(k=4000.0, t=7 / 365, half_spread_volpts=0.01)
    a, b = q(cfg, **kw), q(cfg, fee_cover=3.0, **kw)
    assert b.bid < a.bid and b.ask > a.ask


def test_more_min_ticks_widens(cfg):
    a = q(cfg, half_spread_volpts=0.01, fee_cover=0.0)
    b = q(cfg, half_spread_volpts=0.01, fee_cover=0.0, min_half_width_ticks=3)
    assert b.ask - b.bid > a.ask - a.bid
    assert b.ask - b.bid >= 6 * cfg.specs.option.tick_for(b.bid) - 1e-12


def test_no_bid_when_centre_vol_minus_half_spread_is_not_positive(cfg):
    r = q(cfg, sigma=0.01, half_spread_volpts=1.0)
    assert r.bid is None and r.ask > 0


def test_wide_vol_spread_is_not_floor_bound(cfg):
    assert not q(cfg, half_spread_volpts=3.0).floor_bound


# ---------------- skew ----------------


@pytest.mark.parametrize("nv, sign", [(+2500.0, -1), (-2500.0, +1)])
def test_skew_moves_both_quotes_against_inventory(cfg, nv, sign):
    flat, sk = q(cfg), q(cfg, nv=nv)
    assert np.sign(sk.bid - flat.bid) == sign and np.sign(sk.ask - flat.ask) == sign


def test_skew_is_kappa_times_vega_ratio(cfg):
    r = q(cfg, nv=2500.0, kappa_volpts=1.0, vref=5000.0)
    assert r.sigma_center == pytest.approx(VOL - 0.005)
    assert r.skew_volpts == pytest.approx(-0.5) and not r.skew_capped


def test_stronger_kappa_shifts_more(cfg):
    # 0.2 vs 0.9 vol points: the gap (~0.0008 ETH) exceeds the 0.0005 tick, so rounding
    # cannot hide it (smaller skews can round to the same price)
    weak = q(cfg, nv=5000.0, kappa_volpts=0.2, vref=5000.0)
    strong = q(cfg, nv=5000.0, kappa_volpts=0.9, vref=5000.0)
    assert strong.ask < weak.ask and strong.bid < weak.bid


def test_kappa_zero_ignores_inventory(cfg):
    assert q(cfg, nv=1e6, kappa_volpts=0.0) == q(cfg, kappa_volpts=0.0)


@pytest.mark.parametrize("nv", [1e5, -1e5])
def test_skew_capped_at_half_spread_never_quotes_through_fair(cfg, nv):
    r = q(cfg, nv=nv, half_spread_volpts=1.0, kappa_volpts=2.0)
    fair = price_eth(F, 3300.0, T30, VOL, True)
    assert r.skew_capped and abs(r.skew_volpts) == pytest.approx(1.0)
    assert r.bid <= fair + 1e-12 <= r.ask + 2e-12  # inner quote at most at fair
    assert skew_volpts(nv, cfg.quoting.model_copy(update={"kappa_volpts": 2.0}), 2000.0)[1]


# ---------------- sizing ----------------


def test_vega_sizing(cfg):
    v = vega(F, 3300.0, T30, VOL)
    assert q(cfg, target_vega_usd=50.0, max_contracts=1000).size == math.floor(50.0 / v)
    assert q(cfg, target_vega_usd=1e6, max_contracts=25).size == 25  # cap
    assert q(cfg, target_vega_usd=1e-3) is None  # below the minimum order


def test_higher_target_vega_quotes_more(cfg):
    assert q(cfg, target_vega_usd=80.0).size > q(cfg, target_vega_usd=20.0).size


def test_lower_vega_option_gets_more_contracts(cfg):
    long_atm = q(cfg, t=60 / 365, max_contracts=1000)
    short_wing = q(cfg, k=3700.0, t=7 / 365, max_contracts=1000)
    assert short_wing.size > long_atm.size


def test_fixed_sizing(cfg):
    assert q(cfg, sizing="fixed", fixed_contracts=7).size == 7
    assert quote_size(np.nan, cfg.quoting.model_copy(update={"sizing": "fixed"}),
                      cfg.specs.option) == cfg.quoting.fixed_contracts  # fmt: skip


def test_spot_vega_uses_s_over_f(cfg):
    qc = cfg.quoting.model_copy(update={"max_contracts": 1000})
    a = make_quote(VOL, F, 3300.0, T30, True, 0.0, qc, cfg.specs.option, 2000.0, index_usd=F)
    b = make_quote(VOL, F, 3300.0, T30, True, 0.0, qc, cfg.specs.option, 2000.0, F / 2)
    assert b.size == math.floor(50.0 / (vega(F, 3300.0, T30, VOL) / 2))
    assert b.size > a.size


# ---------------- universe ----------------


@pytest.mark.parametrize(
    "otm, days, ok",
    [(0.30, 30, True), (0.10, 30, True), (0.09, 30, False), (0.30, 1.5, False),
     (0.30, 2.0, True), (0.30, 120, False)],
)  # fmt: skip
def test_universe(cfg, otm, days, ok):
    assert in_universe(otm, days, cfg.quoting.universe) is ok


def test_universe_uses_otm_delta_for_itm_strikes():
    # a deep ITM call (|delta| ~0.95) sits on the put wing: excluded like the OTM put
    d_call, d_put = delta(F, 2000.0, T30, VOL, True), delta(F, 2000.0, T30, VOL, False)
    assert d_call > 0.9
    assert otm_delta(F, 2000.0, T30, VOL) == pytest.approx(min(d_call, -d_put))
    assert otm_delta(F, F, 1.0, 1e-6) == pytest.approx(0.5, abs=1e-6)


# ---------------- book vega ----------------


def test_book_vega_sums_signed_positions_at_spot():
    n, k, s = np.array([10.0, -4.0]), np.array([3000.0, 3600.0]), 3200.0
    exp = 10 * vega(F, k[0], T30, VOL) * s / F - 4 * vega(F, k[1], T30, VOL) * s / F
    assert book_vega_usd(n, F, k, T30, VOL, s) == pytest.approx(exp)
    assert book_vega_usd([1.0], F, 3000.0, T30, np.nan, s) == 0.0  # unvalued: ignored


# ---------------- properties ----------------


@settings(max_examples=400, deadline=None)
@given(
    sigma=st.floats(0.05, 3.0),
    m=st.floats(-1.0, 1.0),
    days=st.floats(0.5, 120),
    call=st.booleans(),
    h=st.floats(0.0, 10.0),
    nv=st.floats(-5e4, 5e4),
    kappa=st.floats(0.0, 3.0),
    sizing=st.sampled_from(["vega", "fixed"]),
)
def test_quote_properties(sigma, m, days, call, h, nv, kappa, sizing):
    k, t = F * math.exp(m), days / 365
    r = q(CFG, k=k, t=t, call=call, sigma=sigma, nv=nv, half_spread_volpts=h,
          kappa_volpts=kappa, sizing=sizing)  # fmt: skip
    if r is None:
        return
    spec = CFG.specs.option
    min_tick = spec.tick_steps[0].tick_eth
    assert r.ask >= min_tick - 1e-12 and on_grid(r.ask, spec)
    if r.bid is not None:
        assert r.bid >= min_tick - 1e-12 and on_grid(r.bid, spec)
        assert r.bid < r.ask
        fair = float(price_eth(F, k, t, sigma, call))
        assert r.bid <= fair + 1e-12 <= r.ask + 2e-12  # skew cap: never through fair
    assert abs(r.skew_volpts) <= h + 1e-9
    assert r.size >= spec.min_order_contracts and r.size == math.floor(r.size)
    if sizing == "vega":
        assert r.size <= CFG.quoting.max_contracts
