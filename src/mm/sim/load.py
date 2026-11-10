"""Load one run's inputs from data/clean and the Surface A cache."""

from __future__ import annotations

import logging

import pandas as pd

from mm.config import Config
from mm.data.db import connect
from mm.pricing.enrich import build_curve
from mm.sim.engine import SimInputs
from mm.sim.fair import SurfaceBook, _ms, fair_values
from mm.surface.build import CACHE

log = logging.getLogger(__name__)
TRADE_COLS = (
    "ts, trade_id, trade_seq, instrument_name, direction, price_eth, amount_contracts, "
    "expiry, strike_usd, option_type, t_years, is_suspect, is_liquidation"
)


def load_inputs(cfg: Config, start: pd.Timestamp, end: pd.Timestamp) -> SimInputs:
    con = connect(cfg.data.clean_dir)
    s, e = start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")
    rng = f"ts >= timestamp '{s}' and ts < timestamp '{e}'"
    trades = con.execute(
        f"select {TRADE_COLS} from option_trades where {rng} order by ts, trade_seq"
    ).df()
    fits = pd.read_parquet(CACHE / "fits.parquet")
    refits = pd.read_parquet(CACHE / "refits.parquet")
    win = (fits["refit_ts"] >= start - pd.Timedelta("1D")) & (fits["refit_ts"] < end)
    book = SurfaceBook(fits[win], _ms(refits["refit_ts"]))
    curve = build_curve(cfg, start.date(), (end + pd.Timedelta("1D")).date())
    dp = con.execute("select settle_ts, delivery_price_usd from delivery_prices").df()
    fund = con.execute(
        f"select available_at, interest_1h, index_price_usd from funding "
        f"where available_at >= timestamp '{s}' and available_at < timestamp '{e}'"
    ).df()
    fund["available_ms"] = _ms(fund["available_at"])
    log.info("trades %d, refits in window %d", len(trades), int(win.sum()))
    return SimInputs(
        trades=fair_values(trades, book, curve, cfg.sim.latency_ms),
        book=book,
        curve=curve,
        delivery_usd=dict(zip(_ms(dp["settle_ts"]), dp["delivery_price_usd"], strict=True)),
        funding=fund,
        activity_ms=curve.activity_ms,
        start_ms=int(start.value // 10**6),
        end_ms=int(end.value // 10**6),
    )
