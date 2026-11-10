"""Phase 6: account delta, inverse perpetual, hedge sign/size/cost, funding sign."""

import numpy as np
import pytest

from mm.hedging.hedger import exec_price, hedge_qty, needs_hedge, perp_fee_eth, perp_pnl_eth
from mm.risk.greeks import account_delta_eth, usd_value
from mm.sim.account import Account, InvariantError
from mm.sim.fees import funding_eth

S = 3000.0
INST = "ETH-10JAN25-3000-C"


def test_inverse_perp_pnl_in_eth_and_usd():
    pnl = perp_pnl_eth(q_usd=3000.0, p0=3000.0, p=3300.0)
    assert pnl == pytest.approx(3000 * (1 / 3000 - 1 / 3300))
    assert pnl * 3300 == pytest.approx(300.0)  # $300 at the new price
    assert perp_pnl_eth(-3000.0, 3000.0, 3300.0) == pytest.approx(-pnl)


def test_perp_delta_is_q_over_p0_by_bump():
    acct = Account(0.0, perp_usd=6000.0, perp_entry=S)

    def w(s):
        return usd_value(acct, s, perp_px=s)  # perp price moves with S

    assert (w(S + 0.5) - w(S - 0.5)) / 1.0 == pytest.approx(6000.0 / S, rel=1e-6)
    assert account_delta_eth(acct, {}) == pytest.approx(2.0)


def test_eth_balance_is_delta_by_bump():
    acct = Account(7.0)
    assert usd_value(acct, S + 1, S) - usd_value(acct, S, S) == pytest.approx(7.0)
    assert account_delta_eth(acct, {}) == 7.0


def test_settlement_resets_p0_and_keeps_delta_and_value():
    acct = Account(10.0, perp_usd=-30_000.0, perp_entry=3000.0)
    d0, w0 = account_delta_eth(acct, {}), usd_value(acct, 3150.0, perp_px=3150.0)
    acct.perp_trade(ts_ms=1, dq_usd=0.0, price=3150.0, fee_eth=0.0)  # settle only
    assert acct.perp_entry == 3150.0
    assert acct.eth == pytest.approx(10.0 + (-30_000.0) * (1 / 3000 - 1 / 3150))
    assert account_delta_eth(acct, {}) == pytest.approx(d0)
    assert usd_value(acct, 3150.0, perp_px=3150.0) == pytest.approx(w0)
    acct.audit()


def test_hedge_trade_posts_pnl_and_fee_and_audits():
    acct = Account(10.0)
    acct.perp_trade(0, -3000.0, 3000.0, 0.0005)
    acct.perp_trade(1, +1000.0, 2900.0, 0.0002)
    assert acct.perp_usd == -2000.0 and acct.perp_entry == 2900.0
    pnl = -3000.0 * (1 / 3000 - 1 / 2900)
    assert acct.eth == pytest.approx(10.0 - 0.0005 + pnl - 0.0002)
    acct.audit()
    acct.perp_usd += 1.0  # changed outside perp_trade
    with pytest.raises(InvariantError):
        acct.audit()


def test_collateral_alone_is_long_delta_and_hedge_sells():
    acct = Account(50.0)
    assert account_delta_eth(acct, {}) == pytest.approx(50.0)
    assert hedge_qty(50.0, S, contract_usd=1.0) == pytest.approx(-150_000.0)


@pytest.mark.parametrize("delta, sign", [(+7.3, -1), (-7.3, +1)])
def test_hedge_sign_and_rounding(delta, sign):
    dq = hedge_qty(delta, S, contract_usd=1.0)
    assert np.sign(dq) == sign and dq == round(dq)
    assert abs(dq + S * delta) <= 0.5


def test_band():
    assert not needs_hedge(4.99, 5.0) and not needs_hedge(-5.0, 5.0)
    assert needs_hedge(5.01, 5.0) and needs_hedge(-5.01, 5.0)


def test_buying_a_call_adds_premium_adjusted_delta():
    acct = Account(10.0)
    acct.option_fill(0, INST, "buy", 1.0, 0.08, 0.0003)
    d = account_delta_eth(acct, {INST: 0.50})
    assert d == pytest.approx(10.0 - 0.08 - 0.0003 + 0.50)


def test_full_hedge_leaves_only_basis_residual():
    acct = Account(0.0)
    delta = 12.0  # e.g. from options
    dq = hedge_qty(delta, S, 1.0)
    acct.perp_trade(0, dq, price=S * 1.0005, fee_eth=0.0)  # exec 5 bp away from S
    assert abs(delta + account_delta_eth(acct, {})) < 12.0 * 1e-3 + 1 / S


def test_hedge_cost_buy_pays_more_and_taker_fee(cfg):
    p = cfg.specs.perp
    assert exec_price(3000.0, +1, bps=1.0) == pytest.approx(3000.3)
    assert exec_price(3000.0, -1, bps=1.0) == pytest.approx(2999.7)
    assert perp_fee_eth(-150_000.0, 3000.0, p) == pytest.approx(0.0005 * 150_000 / 3000)


def test_funding_sign_on_the_hedge():
    # short perp (collateral hedge) receives positive funding, pays negative
    assert funding_eth(-150_000.0, 0.0001, 3000.0) == pytest.approx(+0.005)
    assert funding_eth(-150_000.0, -0.0001, 3000.0) == pytest.approx(-0.005)
