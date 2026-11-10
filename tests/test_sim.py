import numpy as np
import pandas as pd
import pytest

from mm.config import load_config
from mm.pricing.black76 import price_eth, vega
from mm.pricing.forwards import ForwardCurve, future_basis_obs
from mm.quoting.quoter import make_quote
from mm.sim.account import Account, InvariantError
from mm.sim.engine import SimInputs, find_pauses, run
from mm.sim.fair import SurfaceBook, _ms, fair_values
from mm.sim.fees import (
    delivery_fee_eth,
    funding_eth,
    is_daily_expiry,
    option_fee_eth,
    payoff_eth,
)
from mm.sim.fills import ASK_BELOW, BID_ABOVE, QuoteBook, cross, decide

START = pd.Timestamp("2025-01-06 00:00", tz="UTC")
END = pd.Timestamp("2025-01-11 00:00", tz="UTC")
EXP = pd.Timestamp("2025-01-10 08:00", tz="UTC")  # a Friday
INST = "ETH-10JAN25-3000-C"
VOL = 0.60
S = 3000.0
BASE_CFG = load_config(__import__("pathlib").Path(__file__).parents[1] / "config" / "base.yaml")


@pytest.fixture
def opt(cfg):
    return cfg.specs.option


# ---------------- fees, payoff, funding ----------------


def test_maker_fee_capped_at_12_5_percent_of_premium(opt):
    assert option_fee_eth(10, 0.001, opt) == pytest.approx(10 * 0.125 * 0.001)
    assert option_fee_eth(10, 0.05, opt) == pytest.approx(10 * 0.0003)
    assert option_fee_eth(-10, 0.05, opt) == pytest.approx(10 * 0.0003)  # sign-free


def test_call_and_put_payoff_in_eth():
    assert payoff_eth(3000, True, 3300) == pytest.approx(300 / 3300)
    assert payoff_eth(3000, False, 3300) == 0.0
    assert payoff_eth(3000, False, 2500) == pytest.approx(500 / 2500)


def test_delivery_fee_only_in_the_money_capped_and_daily_exempt(opt):
    assert delivery_fee_eth(2, 0.0, opt, daily=False) == 0.0
    assert delivery_fee_eth(2, 0.1, opt, daily=False) == pytest.approx(2 * 0.00015)
    assert delivery_fee_eth(2, 0.0004, opt, daily=False) == pytest.approx(2 * 0.125 * 0.0004)
    assert delivery_fee_eth(2, 0.1, opt, daily=True) == 0.0


def test_daily_expiry_is_any_non_friday():
    assert not is_daily_expiry(int(EXP.value // 10**6))
    assert is_daily_expiry(int(pd.Timestamp("2025-01-08 08:00", tz="UTC").value // 10**6))


def test_funding_sign_long_pays_positive_rate():
    assert funding_eth(30_000, 0.0001, 3000) == pytest.approx(-0.001)
    assert funding_eth(-30_000, 0.0001, 3000) == pytest.approx(0.001)
    assert funding_eth(30_000, -0.0001, 3000) == pytest.approx(0.001)


# ---------------- fills ----------------


def test_taker_sell_fills_bid_only_if_strictly_above_trade_price(cfg):
    f = cfg.fill
    assert cross("sell", 0.0500, 0.0505, 0.06, f)[3] == BID_ABOVE
    assert cross("sell", 0.0500, 0.0500, 0.06, f) is None  # at price: fraction 0 (D003)
    assert cross("sell", 0.0500, 0.0495, 0.06, f) is None
    assert cross("buy", 0.0600, 0.05, 0.0595, f)[3] == ASK_BELOW
    assert cross("buy", 0.0600, 0.05, 0.0600, f) is None


def test_at_price_fraction_applies_when_configured(cfg):
    f = cfg.fill.model_copy(update={"at_price_fill_fraction": 0.25})
    d = decide("X", "sell", 0.05, 4, 0, 0.05, 0.06, 10, QuoteBook(0), f)
    assert d.qty == pytest.approx(4 * 0.25 * 0.5)


def test_fill_size_is_half_of_min_trade_and_quote(cfg):
    book = QuoteBook(0)
    assert decide("X", "sell", 0.05, 4, 0, 0.051, 0.06, 10, book, cfg.fill).qty == 2.0
    assert decide("Y", "sell", 0.05, 40, 0, 0.051, 0.06, 10, book, cfg.fill).qty == 5.0


def test_sweep_cannot_use_more_than_the_quote(cfg):
    book = QuoteBook(0)
    legs = [decide("X", "sell", p, 30, 1000, 0.0505, 0.06, 10, book, cfg.fill)
            for p in (0.050, 0.049, 0.048)]  # fmt: skip
    assert legs[0].qty == 5.0 and legs[1] is None and legs[2] is None
    # replenished at the next, later trade (latency 0)
    assert decide("X", "sell", 0.05, 30, 1001, 0.0505, 0.06, 10, book, cfg.fill).qty == 5.0


def test_quote_replenished_only_after_latency(cfg):
    book = QuoteBook(1000)
    decide("X", "sell", 0.05, 30, 0, 0.0505, 0.06, 10, book, cfg.fill)
    assert decide("X", "sell", 0.05, 30, 1000, 0.0505, 0.06, 10, book, cfg.fill) is None
    assert decide("X", "sell", 0.05, 30, 1001, 0.0505, 0.06, 10, book, cfg.fill).qty == 5.0
    # the other side was never used
    assert decide("X", "buy", 0.07, 30, 500, 0.0505, 0.06, 10, book, cfg.fill).qty == 5.0


def test_random_sizing_is_whole_contracts_with_the_same_mean(cfg):
    qty = [decide("X", "sell", 0.05, 1, i, 0.051, 0.06, 10, QuoteBook(0), cfg.fill,
                  "random", np.random.default_rng([0, i])) for i in range(4000)]  # fmt: skip
    q = np.array([d.qty if d else 0.0 for d in qty])
    assert set(np.unique(q)) <= {0.0, 1.0}
    assert q.mean() == pytest.approx(0.5, abs=0.03)


# ---------------- account ----------------


def test_balance_changes_only_through_known_flows():
    a = Account(10.0)
    with pytest.raises(InvariantError):
        a.post(0, "gift", 1.0)
    with pytest.raises(InvariantError):
        a.move(0, "transfer", INST, 1.0)


def test_audit_catches_a_balance_changed_outside_the_ledger():
    a = Account(10.0)
    a.option_fill(0, INST, "buy", 2, 0.05, 0.0006)
    a.audit()
    a._eth += 0.01
    with pytest.raises(InvariantError):
        a.check()


def test_option_fill_signs():
    a = Account(10.0)
    a.option_fill(0, INST, "buy", 2, 0.05, 0.0006)
    a.option_fill(1, INST, "sell", 0.5, 0.06, 0.00015)
    assert a.positions == {INST: 1.5}
    assert a.eth == pytest.approx(10 - 0.1 - 0.0006 + 0.03 - 0.00015)


# ---------------- synthetic market ----------------


def _ms1(ts) -> int:
    return int(pd.Timestamp(ts).value // 10**6)


def _t(ts) -> float:
    return (EXP - pd.Timestamp(ts, tz="UTC")).total_seconds() / (365 * 86400)


def _quote(cfg, ts, nv=0.0):
    """The quote the engine should post on INST at ts, given net book vega nv."""
    return make_quote(
        VOL, S, 3000.0, _t(ts), True, nv, cfg.quoting, cfg.specs.option, cfg.risk.vega_limit_usd, S
    )


def _with_quoting(cfg, **upd):
    return cfg.model_copy(update={"quoting": cfg.quoting.model_copy(update=upd)})


def _market(cfg, vol=VOL, extra_refit_skip=(), b=0.0, rho=0.0):
    """60% ATM smile (flat unless b > 0), forward = index = 3000, refits every 5 min."""
    step = cfg.surface.refit_interval_s
    grid = pd.date_range(START, END, freq=f"{step}s")
    t_ref = (EXP - grid).total_seconds() / (365 * 86400)
    live = (t_ref > 0) & ~grid.isin(list(extra_refit_skip))
    fits = pd.DataFrame(
        {
            "refit_ts": grid[live],
            "expiry": EXP,
            "t_ref": t_ref[live],
            "kind": "full",
            "a": vol**2 * t_ref[live] - b * 0.1,  # w(0) = vol^2 t_ref (m = 0, s = 0.1)
            "b": b,
            "rho": rho,
            "m": 0.0,
            "s": 0.1,
        }
    )
    obs_ts = pd.date_range(START - pd.Timedelta("1D"), END, freq="10min")
    fut = pd.DataFrame(
        {
            "ts": obs_ts,
            "instrument_name": "ETH-10JAN25",
            "expiry": EXP,
            "mark_price_usd": S,
            "index_price_usd": S,
        }
    )
    curve = ForwardCurve(
        fut[["ts", "index_price_usd"]],
        future_basis_obs(fut),
        pd.DataFrame(columns=["ts", "expiry", "basis"]),
        cfg.pricing.forwards,
    )
    book = SurfaceBook(fits, _ms(pd.Series(grid)))
    return book, curve


def _trades(rows):
    if not rows:  # an empty frame with the right columns
        return _trades([("2025-01-06 00:00", "sell", 0.01, 1)]).iloc[:0]
    out = []
    for n, (ts, direction, px, qty) in enumerate(rows):
        ts = pd.Timestamp(ts, tz="UTC")
        out.append(
            {
                "ts": ts,
                "trade_id": f"T{n}",
                "trade_seq": n,
                "instrument_name": INST,
                "direction": direction,
                "price_eth": px,
                "amount_contracts": qty,
                "expiry": EXP,
                "strike_usd": 3000.0,
                "option_type": "C",
                "t_years": (EXP - ts).total_seconds() / (365 * 86400),
                "is_suspect": False,
                "is_liquidation": False,
            }
        )
    return pd.DataFrame(out)


def _perp(rows):
    """rows: (ts, taker direction, price); index = mark = price."""
    return pd.DataFrame(
        {
            "ts_ms": [_ms1(t) for t, _, _ in rows],
            "direction": [d for _, d, _ in rows],
            "price_usd": [p for *_, p in rows],
            "mark_price_usd": [p for *_, p in rows],
            "index_price_usd": [p for *_, p in rows],
        }
    )


def _with(cfg, section, **upd):
    return cfg.model_copy(update={section: getattr(cfg, section).model_copy(update=upd)})


def _inputs(cfg, trades, book, curve, delivery=3300.0, activity=None, perp=None):
    return SimInputs(
        trades=fair_values(trades, book, curve, cfg.sim.latency_ms),
        book=book,
        curve=curve,
        delivery_usd={_ms1(EXP): delivery},
        funding=pd.DataFrame(columns=["available_ms", "interest_1h", "index_price_usd"]),
        activity_ms=np.array([], "int64") if activity is None else activity,
        start_ms=_ms1(START),
        end_ms=_ms1(END),
        perp=perp,
    )


def test_fair_value_ignores_refit_after_cutoff(cfg):
    # the 12:00 refit is missing: a trade at 12:02 must use 11:55, never 12:05
    book, curve = _market(cfg, extra_refit_skip=[pd.Timestamp("2025-01-06 12:00", tz="UTC")])
    fv = fair_values(_trades([("2025-01-06 12:02", "sell", 0.01, 1)]), book, curve, 0)
    assert np.isnan(fv["sigma_fair"].iloc[0])  # the latest refit (12:00) has no slice
    fv = fair_values(_trades([("2025-01-06 12:07", "sell", 0.01, 1)]), book, curve, 0)
    assert pd.Timestamp(fv["refit_ms"].iloc[0], unit="ms", tz="UTC").minute == 5
    assert fv["sigma_fair"].iloc[0] == pytest.approx(VOL)


def test_latency_moves_the_refit_back(cfg):
    book, curve = _market(cfg)
    fv = fair_values(_trades([("2025-01-06 12:05:00.5", "sell", 0.01, 1)]), book, curve, 1000)
    assert pd.Timestamp(fv["refit_ms"].iloc[0], unit="ms", tz="UTC").minute == 0


def test_synthetic_run_known_outcome(cfg):
    cfg = _with_quoting(cfg, kappa_volpts=0.0)  # skew has its own tests below
    book, curve = _market(cfg)
    # t3 is 2.6 days before expiry, inside the 2-day universe cut-off
    t1, t2, t3 = "2025-01-06 12:00:30", "2025-01-07 12:00:30", "2025-01-07 18:00:30"
    bid1, ask2, bid3 = _quote(cfg, t1).bid, _quote(cfg, t2).ask, _quote(cfg, t3).bid
    trades = _trades(
        [
            (t1, "sell", bid1 - 0.002, 4),  # below our bid: we buy 0.5 x 4 = 2
            (t2, "buy", ask2 + 0.002, 2),  # above our ask: we sell 0.5 x 2 = 1
            (t3, "sell", bid3, 6),  # exactly at our bid: no fill
        ]
    )
    res = run(_inputs(cfg, trades, book, curve), cfg)
    f = res.fills
    assert list(f["reason"]) == [BID_ABOVE, ASK_BELOW]
    assert list(f["qty"]) == [2.0, 1.0]
    assert list(f["fill_price"]) == pytest.approx([bid1, ask2])
    fee1, fee2 = 2 * min(0.0003, 0.125 * bid1), 1 * min(0.0003, 0.125 * ask2)
    settle = 1 * 300 / 3300 - 0.00015
    expected = 50.0 - 2 * bid1 - fee1 + ask2 - fee2 + settle
    assert res.account.eth == pytest.approx(expected, abs=1e-12)
    assert res.account.positions == {}
    kinds = res.ledger.groupby("kind")["eth"].sum()
    assert kinds["settlement"] == pytest.approx(300 / 3300)
    assert kinds["delivery_fee"] == pytest.approx(-0.00015)
    assert res.skips["no_cross"] == 1


def test_pause_blocks_quotes_until_next_refit(cfg):
    book, curve = _market(cfg)
    t0 = _ms1("2025-01-06 11:50")
    activity = np.r_[np.arange(t0, t0 + 300_000, 1000), np.arange(t0 + 900_000, t0 + 2e6, 1000)]
    p = find_pauses(activity.astype("int64"), cfg.sim.pause_gap_s, book.grid)
    assert len(p) == 1 and p["quote_from_ms"].iloc[0] == _ms1("2025-01-06 12:10")
    tr = _trades([("2025-01-06 12:06", "sell", 0.0001, 4), ("2025-01-06 12:11", "sell", 0.0001, 4)])
    res = run(_inputs(cfg, tr, book, curve, activity=activity.astype("int64")), cfg)
    assert res.skips["paused"] == 1 and len(res.fills) == 1


def test_short_position_settles_with_negative_payoff(cfg):
    book, curve = _market(cfg)
    ts = "2025-01-06 12:00:30"
    ask = _quote(cfg, ts).ask
    res = run(_inputs(cfg, _trades([(ts, "buy", ask + 0.01, 4)]), book, curve), cfg)
    settle = res.ledger.query("kind == 'settlement'")["eth"].sum()
    assert settle == pytest.approx(-2 * 300 / 3300)
    assert res.account.positions == {}


def test_daily_snapshots_value_open_options_at_surface_a(cfg):
    book, curve = _market(cfg)
    ts = "2025-01-06 12:00:30"
    bid = _quote(cfg, ts).bid
    res = run(_inputs(cfg, _trades([(ts, "sell", bid - 0.002, 4)]), book, curve), cfg)
    d = res.daily.set_index("ts_ms").iloc[0]  # 2025-01-07 00:00
    t_snap = (EXP - pd.Timestamp("2025-01-07", tz="UTC")).total_seconds() / (365 * 86400)
    assert d["options_eth"] == pytest.approx(2 * price_eth(S, 3000.0, t_snap, VOL, True))
    assert d["equity_usd"] == pytest.approx(S * (d["eth_balance"] + d["options_eth"]))


# ---------------- Phase 5: universe, net vega, skew in the loop ----------------


def test_universe_cutoffs_skip_trades(cfg):
    book, curve = _market(cfg)
    tr = _trades([("2025-01-08 12:00:30", "sell", 0.0001, 4)])  # 1.8 days: below 2
    res = run(_inputs(cfg, tr, book, curve), cfg)
    assert res.skips["tenor"] == 1 and res.fills.empty
    far = _trades([("2025-01-06 12:00:30", "sell", 0.0001, 4)]).assign(
        instrument_name="ETH-10JAN25-4500-C", strike_usd=4500.0
    )  # ~0 delta at 60% vol, 4 days
    res = run(_inputs(cfg, far, book, curve), cfg)
    assert res.skips["low_delta"] == 1 and res.fills.empty


def test_net_vega_tracks_fills_and_is_recomputed_at_refits(cfg):
    book, curve = _market(cfg)
    t1, t2, t3 = "2025-01-06 12:00:30", "2025-01-06 12:01:30", "2025-01-06 12:10:30"
    tr = _trades([(t1, "sell", 0.0001, 4), (t2, "buy", 0.5, 2), (t3, "buy", 0.5, 2)])
    inp = _inputs(cfg, tr, book, curve)
    f = run(inp, cfg).fills
    assert list(f["our_side"]) == ["buy", "sell", "sell"]
    assert f["net_vega_usd"].iloc[0] == 0.0
    v1 = inp.trades["vega_usd"].iloc[0]
    assert f["net_vega_usd"].iloc[1] == pytest.approx(2 * v1)  # same refit: + fill vega
    t_refit = _t("2025-01-06 12:10:00")
    assert f["net_vega_usd"].iloc[2] == pytest.approx(1 * vega(S, 3000.0, t_refit, VOL))


def test_long_vega_book_lowers_both_quotes(cfg):
    book, curve = _market(cfg)
    t1, t2 = "2025-01-06 12:00:30", "2025-01-06 12:01:30"
    tr = _trades([(t1, "sell", 0.0001, 4), (t2, "buy", 0.5, 2)])
    flat = run(_inputs(cfg, tr, book, curve), _with_quoting(cfg, kappa_volpts=0.0)).fills
    skc = _with_quoting(cfg, kappa_volpts=1000.0)  # ~2.4 / 2000 x 1000 = 1.2 > h: capped
    sk = run(_inputs(cfg, tr, book, curve), skc).fills
    assert sk["ask"].iloc[1] < flat["ask"].iloc[1] and sk["bid"].iloc[1] < flat["bid"].iloc[1]
    assert sk["skew_capped"].iloc[1] and sk["skew_volpts"].iloc[1] == pytest.approx(-1.0)
    expected = _quote(skc, t2, nv=sk["net_vega_usd"].iloc[1])
    assert sk["ask"].iloc[1] == pytest.approx(expected.ask)


# ---------------- Phase 6: hedging and limits in the loop ----------------


def test_hedge_executes_at_the_next_same_side_print_after_latency(cfg):
    book, curve = _market(cfg)
    cfg1 = _with(cfg, "sim", latency_ms=1000)
    perp = _perp([
        ("2025-01-06 12:00:00", "buy", 3000.0),  # trigger: 50 ETH collateral, delta 50
        ("2025-01-06 12:00:00.5", "sell", 3001.0),  # too early (latency 1 s)
        ("2025-01-06 12:00:01.5", "buy", 3005.0),  # wrong side: a sell hedge gets the bid
        ("2025-01-06 12:00:02", "sell", 3010.0),  # executes here
    ])  # fmt: skip
    res = run(_inputs(cfg1, _trades([]), book, curve, perp=perp), cfg1)
    h = res.hedges.iloc[0]
    assert len(res.hedges) == 1
    assert h.exec_ms == _ms1("2025-01-06 12:00:02") and h.dq_usd == -150_000.0
    px = 3010.0 * (1 - 1e-4)
    assert h.exec_usd == pytest.approx(px)
    assert h.fee_eth == pytest.approx(0.0005 * 150_000 / px)
    assert res.account.perp_usd == -150_000.0 and res.account.perp_entry == pytest.approx(px)
    assert res.ledger.query("kind == 'perp_fee'")["eth"].sum() == pytest.approx(-h.fee_eth)


def test_zero_latency_hedge_never_trades_at_the_trigger_print(cfg):
    book, curve = _market(cfg)
    perp = _perp([("2025-01-06 12:00:00", "sell", 3000.0), ("2025-01-06 12:00:00", "sell", 2999.0)])
    res = run(_inputs(cfg, _trades([]), book, curve, perp=perp), cfg)
    assert res.hedges["print_usd"].tolist() == [2999.0]


def test_no_hedge_inside_the_band(cfg):
    book, curve = _market(cfg)
    c = _with(cfg, "sim", initial_eth=4.0)
    perp = _perp([("2025-01-06 12:00:00", "sell", 3000.0), ("2025-01-06 12:00:01", "sell", 3000.0)])
    res = run(_inputs(c, _trades([]), book, curve, perp=perp), c)
    assert res.hedges.empty and (res.delta["delta_eth"] == 4.0).all()


def test_delta_stays_in_band_through_a_synthetic_crash(cfg):
    # sell 50 ATM calls with quiet prints around them, then S falls 3000 -> 2400 over
    # 2,000 prints with alternating taker sides
    book, curve = _market(cfg)
    sells = [(f"2025-01-06 12:0{m}:30", "buy", 0.5, 100) for m in range(4)]
    t0 = pd.Timestamp("2025-01-06 11:59", tz="UTC")
    rows = [(t0 + pd.Timedelta(seconds=5 * i), "buy" if i % 2 else "sell", 3000.0)
            for i in range(720)]  # fmt: skip
    crash = pd.Timestamp("2025-01-06 13:00", tz="UTC")
    rows += [
        (crash + pd.Timedelta(seconds=3.6 * i), "buy" if i % 2 else "sell", 3000.0 - 0.3 * i)
        for i in range(2001)
    ]  # fmt: skip
    res = run(_inputs(cfg, _trades(sells), book, curve, perp=_perp(rows)), cfg)
    assert res.account.positions == {}  # settled at expiry
    assert res.fills["qty"].sum() == pytest.approx(50.0)
    band = cfg.hedging.delta_band_eth
    d = res.delta[res.delta["ts_ms"] >= _ms1(crash)]["delta_eth"].abs()
    in_crash = res.hedges[res.hedges["decide_ms"] >= _ms1(crash)]
    assert len(in_crash) >= 3  # the crash forced several hedges
    assert (d <= band).mean() > 0.99
    assert d.max() < band + 0.1  # breaches are caught at the next print
    assert (res.hedges["delta_before"].abs() > band).all()
    assert (in_crash["wait_ms"] <= 7_200).all()  # next same-side print (every 7.2 s)


def test_vega_limit_cuts_then_removes_the_bid(cfg):
    book, curve = _market(cfg)
    c = _with(cfg, "risk", vega_limit_usd=3.0)  # ~2.5 contracts of this option
    tr = _trades([(f"2025-01-06 12:0{m}:30", "sell", 0.0001, 4) for m in range(3)])
    inp = _inputs(c, tr, book, curve)
    res = run(inp, c)
    v = inp.trades["vega_usd"].iloc[0]
    assert res.fills["bid_size"].tolist() == [2.0, 1.0]  # floor(3/v)=2, floor((3-v)/v)=1
    assert res.fills["qty"].tolist() == [1.0, 0.5]
    assert res.limits["bid_dropped"] == 1 and res.limits["bid_cut"] == 2
    last = res.quotes.iloc[-1]
    assert last.bid_size == 0.0 and last.ask_size > 0  # only the risk-reducing side left
    assert 1.5 * v <= c.risk.vega_limit_usd


def test_book_delta_matches_bump_and_revalue_on_a_flat_smile(cfg):
    # flat smile: no smile delta, so Black delta = dW/dS by revaluation
    from mm.sim.delta import OptionDelta, option_value_eth
    from mm.sim.engine import _Meta

    book, curve = _market(cfg)
    acct = Account(3.0)
    acct.option_fill(0, INST, "sell", 7.0, 0.05, 0.0)
    acct.option_fill(0, "ETH-10JAN25-3300-P", "buy", 4.0, 0.08, 0.0)
    ts = _ms1("2025-01-07 12:00")
    od = OptionDelta(book, curve, _Meta())
    s = 3100.0

    def w(x):
        return x * (acct.eth + option_value_eth(od, acct, ts, x))

    bump = (w(s + 0.5) - w(s - 0.5)) / 1.0
    assert acct.eth + od.total(acct, ts, s) == pytest.approx(bump, rel=1e-6)
    assert od.n_missing == 0


@pytest.mark.parametrize("mode", ["smile", "black"])
def test_book_delta_matches_its_own_bump_on_a_skewed_smile(cfg, mode):
    # smile delta = bump with the smile read at the new moneyness (sticky moneyness, D038);
    # black delta = bump with each option's vol frozen
    from mm.pricing.black76 import price_eth
    from mm.pricing.time import MS_PER_YEAR
    from mm.sim.delta import OptionDelta, option_value_eth
    from mm.sim.engine import _Meta

    book, curve = _market(cfg, b=0.004, rho=-0.6)  # steep put skew
    acct = Account(3.0)
    acct.option_fill(0, "ETH-10JAN25-2700-P", "buy", 9.0, 0.02, 0.0)
    acct.option_fill(0, "ETH-10JAN25-3300-C", "sell", 6.0, 0.02, 0.0)
    ts, s = _ms1("2025-01-07 12:00"), 3050.0
    od = OptionDelta(book, curve, _Meta(), mode)
    d = acct.eth + od.total(acct, ts, s)
    if mode == "smile":

        def w(x):
            return x * (acct.eth + option_value_eth(od, acct, ts, x))
    else:
        f0 = od._basis * s
        xx = np.log(od._k / f0) - od._m
        sig0 = np.sqrt((od._a + od._b * (od._rho * xx + np.sqrt(xx * xx + od._s**2))) / od._tref)
        t = (od._exp - ts) / MS_PER_YEAR

        def w(x):
            return x * (acct.eth + float(np.dot(od._n, price_eth(od._basis * x, od._k, t, sig0,
                                                                  ~od._put))))  # fmt: skip

    assert d == pytest.approx((w(s + 0.05) - w(s - 0.05)) / 0.1, rel=1e-5)


def test_smile_term_makes_a_risk_reversal_longer_delta(cfg):
    # long put wing + short call wing on a put-skewed smile: smile delta > black delta
    from mm.sim.delta import OptionDelta
    from mm.sim.engine import _Meta

    book, curve = _market(cfg, b=0.004, rho=-0.6)
    acct = Account(0.0)
    acct.option_fill(0, "ETH-10JAN25-2700-P", "buy", 9.0, 0.02, 0.0)
    acct.option_fill(0, "ETH-10JAN25-3300-C", "sell", 6.0, 0.02, 0.0)
    ts = _ms1("2025-01-07 12:00")
    smile = OptionDelta(book, curve, _Meta(), "smile").total(acct, ts, 3050.0)
    black = OptionDelta(book, curve, _Meta(), "black").total(acct, ts, 3050.0)
    assert smile > black
