"""Phase 6: vega and position limits cut the risk-increasing side."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mm.config import load_config
from mm.quoting.quoter import apply_limits, make_quote, round_size
from mm.risk.limits import side_room

CFG = load_config(__import__("pathlib").Path(__file__).parents[1] / "config" / "base.yaml")


def test_vega_limit_drops_the_risk_increasing_side(cfg):
    r = cfg.risk  # vega limit 2000, position limit 100
    bid, ask = side_room(net_vega_usd=1990.0, vega_i=2.0, pos=0.0, r=r)
    assert bid == pytest.approx(5.0)  # (2000 - 1990) / 2
    assert ask == pytest.approx(100.0)  # vega room 1995, position limit binds
    assert side_room(2100.0, 2.0, 0.0, r)[0] <= 0  # breached by drift: no bid
    assert side_room(-2100.0, 2.0, 0.0, r)[1] <= 0  # short vega breached: no ask


def test_position_limit_shrinks_size(cfg):
    bid, ask = side_room(net_vega_usd=0.0, vega_i=2.0, pos=90.0, r=cfg.risk)
    assert bid == pytest.approx(10.0) and ask == pytest.approx(190.0)


def test_apply_limits_cuts_then_drops_a_side(cfg):
    spec = cfg.specs.option
    q0 = make_quote(0.7, 3300.0, 3300.0, 30 / 365, True, 0.0, cfg.quoting, spec, 2000.0)
    cut = apply_limits(q0, 5.7, 100.0, spec)
    assert cut.bid_size == 5.0 and cut.ask_size == q0.size and cut.bid == q0.bid
    gone = apply_limits(q0, 0.6, -3.0, spec)
    assert gone.bid is None and gone.ask is None and gone.bid_size == gone.ask_size == 0.0


@settings(max_examples=200, deadline=None)
@given(
    fills=st.lists(st.tuples(st.sampled_from(["buy", "sell"]), st.floats(0.1, 500)), max_size=200),
    vega_i=st.floats(0.2, 30.0),
)
def test_fills_never_breach_limits(fills, vega_i):
    # random taker flow against limit-sized quotes, vega fixed: limits always hold
    r, spec, frac = CFG.risk, CFG.specs.option, CFG.fill.max_fill_fraction
    pos = nv = 0.0
    for taker, trade in fills:
        bid_room, ask_room = side_room(nv, vega_i, pos, r)
        room = bid_room if taker == "sell" else ask_room  # a taker sell hits our bid
        size = round_size(min(25.0, room), spec)
        if size is None:
            continue
        q = min(trade, size) * frac * (1 if taker == "sell" else -1)
        pos, nv = pos + q, nv + q * vega_i
        assert abs(pos) <= r.max_position_contracts + 1e-9
        assert abs(nv) <= r.vega_limit_usd + 1e-9
