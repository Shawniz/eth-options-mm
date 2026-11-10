"""Phase 7: waterfall attribution, flows, replay, residual and markouts."""

import numpy as np
import pandas as pd
import pytest
from test_sim import EXP, _inputs, _market, _ms1, _perp, _trades

from mm.pnl.attribution import COMPONENTS, attribute, waterfall
from mm.pnl.markouts import markouts
from mm.pnl.value import Book, Market, MarketData, delta_eth, value_usd
from mm.pricing.black76 import theta as b_theta
from mm.pricing.black76 import vega as b_vega
from mm.pricing.time import MS_PER_YEAR
from mm.sim.engine import run

S, VOL = 3000.0, 0.60
EXP_MS = _ms1(EXP)
T0 = _ms1("2025-01-07 12:00")
HOUR = 3_600_000


def _book(n=(-10.0, -6.0), k=(3000.0, 3200.0), eth=2.0, q=0.0, p0=0.0):
    return Book(eth, q, p0, tuple(f"X{i}" for i in range(len(n))), np.array(n, float),
                np.full(len(n), EXP_MS, "int64"), np.array(k, float),
                np.zeros(len(n), bool))  # fmt: skip


def _mkt(bk, ts=T0, s=S, vol=VOL, basis=1.0, pi=1.0):
    t_ref = (EXP_MS - ts) / MS_PER_YEAR
    par = np.tile([vol**2 * t_ref, 0.0, 0.0, 0.0, 0.1, t_ref], (len(bk.n), 1))
    return Market(ts, s, pi, np.full(len(bk.n), basis), par)


def _t(ts):
    return (EXP_MS - ts) / MS_PER_YEAR


# ---------------- waterfall on known moves ----------------


def test_pure_spot_move_is_delta_plus_half_gamma_ds2():
    bk = _book()
    m0 = _mkt(bk)
    a = waterfall(bk, m0, m0.with_(s=S + 30.0))
    assert a["theta"] == a["basis"] == a["vega"] == 0.0
    assert a["delta"] == pytest.approx(delta_eth(bk, m0) * 30.0)
    h = 1.0
    gam = (value_usd(bk, m0.with_(s=S + h)) - 2 * value_usd(bk, m0)
           + value_usd(bk, m0.with_(s=S - h))) / h**2  # fmt: skip
    assert a["gamma"] == pytest.approx(0.5 * gam * 30.0**2, rel=0.03)
    assert a["gamma"] < 0  # short options


def test_pure_time_is_theta():
    bk = _book()
    m0 = _mkt(bk)
    a = waterfall(bk, m0, m0.with_(ts=T0 + HOUR))
    assert a["delta"] == a["gamma"] == a["basis"] == a["vega"] == 0.0
    th = float(np.dot(bk.n, b_theta(S, bk.k, _t(T0), VOL)))  # USD per day (S = F)
    assert a["theta"] == pytest.approx(th / 24, rel=0.01)
    assert a["theta"] > 0  # short options earn theta


def test_vol_shift_is_vega():
    bk = _book()
    m0 = _mkt(bk)
    m1 = m0.with_(par=_mkt(bk, vol=VOL + 0.01).par)  # +1 vol point at a refit
    a = waterfall(bk, m0, m1)
    v = float(np.dot(bk.n, b_vega(S, bk.k, _t(T0), VOL)))
    assert a["vega"] == pytest.approx(v, rel=0.02) and a["vega"] < 0
    assert a["theta"] == a["delta"] == a["gamma"] == a["basis"] == 0.0


def test_basis_and_perp_premium_land_in_basis_only():
    bk = _book(q=-30_000.0, p0=2990.0)
    m0 = _mkt(bk)
    a = waterfall(bk, m0, m0.with_(basis=np.full(2, 1.002), pi=0.999))
    assert a["basis"] != 0.0
    assert a["theta"] == a["delta"] == a["gamma"] == a["vega"] == 0.0


def test_steps_add_up_exactly():
    bk = _book(q=-30_000.0, p0=2990.0)
    m0 = _mkt(bk)
    m1 = Market(T0 + HOUR, 2950.0, 0.998, np.full(2, 1.001), _mkt(bk, ts=T0 + HOUR, vol=0.63).par)
    a = waterfall(bk, m0, m1)
    parts = sum(a[k] for k in ("theta", "delta", "gamma", "basis", "vega"))
    assert parts == pytest.approx(a["w1"] - a["w0"], abs=1e-8)


def test_perp_settlement_is_value_neutral():
    bk = _book(q=-30_000.0, p0=2990.0)
    m = _mkt(bk, pi=1.0005)
    p = m.pi * m.s
    settled = Book(bk.eth + bk.perp_usd * (1 / bk.perp_entry - 1 / p), bk.perp_usd, p,
                   bk.names, bk.n, bk.exp, bk.k, bk.put)  # fmt: skip
    assert value_usd(settled, m) == pytest.approx(value_usd(bk, m))


def test_delta_by_bump_includes_balance_options_and_perp():
    bk = _book(n=(), k=(), eth=5.0, q=6000.0, p0=3000.0)
    assert delta_eth(bk, _mkt(bk)) == pytest.approx(5.0 + 6000.0 / 3000.0, rel=1e-6)


# ---------------- flows and reconciliation on a synthetic run ----------------


def _run_and_attribute(cfg, trades, perp=None, funding=None, **mkt):
    book, curve = _market(cfg, **mkt)
    inp = _inputs(cfg, trades, book, curve, perp=perp)
    if funding is not None:
        inp.funding = funding
    res = run(inp, cfg)
    md = MarketData(inp.book, inp.curve, inp.perp)
    att = attribute(res.fills, res.hedges, res.ledger, res.daily, md, cfg.sim.initial_eth,
                    inp.start_ms, inp.end_ms)  # fmt: skip
    return inp, res, att, md


def test_one_fill_no_market_move_spread_capture_equals_edge_equals_markout(cfg):
    inp, res, att, md = _run_and_attribute(cfg, _trades([("2025-01-06 12:00:30", "sell",
                                                          0.0001, 4)]))  # fmt: skip
    edge_usd = res.fills["edge_eth"].iloc[0] * S
    assert att.rows["spread_capture"].sum() == pytest.approx(edge_usd, rel=1e-9)
    mk = markouts(res.fills, md, inp.end_ms)
    assert mk["hedged_0"].iloc[0] == pytest.approx(edge_usd, rel=1e-9)
    assert mk["raw_0"].iloc[0] == pytest.approx(edge_usd, rel=1e-9)
    # after 1 minute only time has passed: markout = edge + 1 minute of theta
    t = _t(res.fills["ts_ms"].iloc[0])
    th_min = 2.0 * float(b_theta(S, 3000.0, t, VOL)) / 1440  # long 2 calls
    assert mk["hedged_1min"].iloc[0] == pytest.approx(edge_usd + th_min, abs=abs(th_min) * 0.05)


def test_components_reconcile_with_hedges_expiry_and_funding(cfg):
    sells = [(f"2025-01-06 12:0{m}:30", "buy", 0.5, 100) for m in range(4)]
    t0 = pd.Timestamp("2025-01-06 11:59", tz="UTC")
    rows = [(t0 + pd.Timedelta(seconds=5 * i), "buy" if i % 2 else "sell", 3000.0 - 0.1 * i)
            for i in range(3000)]  # fmt: skip
    fund = pd.DataFrame({
        "available_ms": [_ms1("2025-01-06 16:00"), _ms1("2025-01-07 16:00")],
        "interest_1h": [0.0001, -0.00005],
        "index_price_usd": [2900.0, 2900.0],
    })  # fmt: skip
    inp, res, att, _ = _run_and_attribute(cfg, _trades(sells), perp=_perp(rows), funding=fund)
    assert len(res.hedges) > 3 and res.ledger["kind"].eq("funding").sum() == 2
    assert res.ledger["kind"].eq("settlement").any()  # the 10 Jan expiry settled
    snaps = att.snapshots
    assert np.allclose(snaps["engine_w"], snaps["replay_w"], atol=1e-6)
    assert np.allclose(snaps["engine_eth"], snaps["replay_eth"], atol=1e-9)
    assert (snaps["engine_n"] == snaps["replay_n"]).all()
    total = snaps["engine_w"].iloc[-1] - att.w_start
    assert att.rows[COMPONENTS].to_numpy().sum() == pytest.approx(total, abs=1e-6)
    # hedge cost = the 1 bp against the print (synthetic mark = print), in USD at the
    # valuation index (the synthetic curve's index is a constant S)
    exp_cost = -(res.hedges["slippage_eth"] * S).sum()
    assert att.rows["hedge_cost"].sum() == pytest.approx(exp_cost, rel=1e-6)
    assert att.rows["perp_fee"].sum() == pytest.approx(-res.hedges["fee_eth"].sum() * S, rel=1e-9)
    fl = res.ledger.query("kind == 'funding'")["eth"].sum()
    assert att.rows["funding"].sum() == pytest.approx(fl * S, rel=1e-9)


def test_settlement_difference_is_payoff_minus_model_value(cfg):
    # short 2 calls at K=3000; the model forward at expiry is 3000 (intrinsic 0) and the
    # delivery price is 3300: settlement difference = -2 x 300/3300 ETH x S
    inp, res, att, _ = _run_and_attribute(cfg, _trades([("2025-01-06 12:00:30", "buy", 0.5, 4)]))
    assert att.rows["settlement"].sum() == pytest.approx(-2 * 300 / 3300 * S, rel=1e-9)


def test_markout_uses_only_the_future_it_names(cfg):
    # the surface jumps +5 vol points at 12:30: a 12:00:30 fill sees it at 1 h, not 5 min
    trades = _trades([("2025-01-06 12:00:30", "sell", 0.0001, 4)])
    inp, res, att, md = _run_and_attribute(cfg, trades, vol_after=("2025-01-06 12:30", 0.65))
    mk = markouts(res.fills, md, inp.end_ms)
    v = 2.0 * float(b_vega(S, 3000.0, _t(_ms1("2025-01-06 13:00")), VOL))
    assert mk["hedged_5min"].iloc[0] - mk["hedged_0"].iloc[0] < 0.05 * v  # theta only
    assert mk["hedged_1h"].iloc[0] - mk["hedged_0"].iloc[0] > 4 * v  # ~ +5 vol points
    assert att.rows["vega"].sum() == pytest.approx(5 * v, rel=0.05)
