"""Our pricer must reproduce Deribit's own marks and displayed Greeks.

Fixture: a one-off snapshot of live tickers (scripts/make_deribit_fixture.py, approved by
the owner). Inputs are Deribit's displayed underlying_price (the forward) and mark_iv.

Tolerances (decision D016):
- Price: Deribit rounds marks to 1e-4 ETH, and each ticker's displayed forward can differ
  by a few bp from the one behind its mark (they are stamped at slightly different
  moments). In ETH terms dp/dlnF = delta - p, so a forward gap of eps moves the price by
  |delta - p| * eps. We allow 1e-4 + |delta - p| * 6 bp, and require the median implied
  forward gap to be <= 1 bp.
- Greeks: Deribit rounds to 5 dp. Theta is compared only for T >= 7 days: for shorter
  expiries Deribit displays theta on a different convention (ours is the analytic
  derivative, checked by finite differences in test_black76.py).
"""

import json
from pathlib import Path

import numpy as np
import pytest

from mm.pricing.black76 import delta, gamma, price_eth, theta, vega

FIXTURE = Path(__file__).parent / "fixtures" / "deribit_tickers_2026-11-08.json"
ROWS = json.loads(FIXTURE.read_text())
FWD_EPS = 6e-4  # 6 bp
ROUND_PX = 1e-4


def _inputs(r):
    return (
        r["underlying_price"],
        r["strike"],
        r["t_years"],
        r["mark_iv"] / 100,
        r["option_type"] == "call",
    )


def test_fixture_is_broad():
    assert len(ROWS) >= 100
    assert len({r["expiry_ms"] for r in ROWS}) >= 10
    assert {r["option_type"] for r in ROWS} == {"call", "put"}
    assert all(r["interest_rate"] == 0.0 for r in ROWS)  # undiscounted, as assumed


@pytest.mark.parametrize("r", ROWS, ids=[r["instrument_name"] for r in ROWS])
def test_reproduces_deribit_mark(r):
    f, k, t, s, call = _inputs(r)
    p = price_eth(f, k, t, s, call)
    sens = abs(delta(f, k, t, s, call) - p)  # dp / dlnF in ETH
    assert abs(p - r["mark_price"]) <= ROUND_PX + sens * FWD_EPS


def test_median_forward_gap_is_tiny():
    gaps = []
    for r in ROWS:
        f, k, t, s, call = _inputs(r)
        p = price_eth(f, k, t, s, call)
        sens = abs(delta(f, k, t, s, call) - p)
        if r["mark_price"] >= 0.005 and sens > 0.05:  # forward identifiable above rounding
            gaps.append(abs(p - r["mark_price"]) / sens)
    assert np.median(gaps) <= 1e-4


@pytest.mark.parametrize("r", ROWS, ids=[r["instrument_name"] for r in ROWS])
def test_reproduces_deribit_greeks(r):
    f, k, t, s, call = _inputs(r)
    g = r["greeks"]
    assert delta(f, k, t, s, call) == pytest.approx(g["delta"], abs=5e-5)
    assert gamma(f, k, t, s) == pytest.approx(g["gamma"], abs=1e-5)
    assert vega(f, k, t, s) == pytest.approx(g["vega"], rel=1e-3, abs=1e-4)
    if t * 365 >= 7:
        assert theta(f, k, t, s) == pytest.approx(g["theta"], rel=1e-3, abs=1e-4)
