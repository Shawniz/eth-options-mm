"""Price every clean option trade: forward, our trade IV and our mark IV.

    uv run python -m mm.pricing.enrich --config config/base.yaml

Reads data/clean, writes data/cache/option_trades_priced/date=YYYY-MM-DD/part.parquet
(strategy-independent work is cached; CLAUDE.md). Uses latency = config sim.latency_ms.

Added columns (decision D018):
  inclusive (<= t, each trade's own index): what Deribit saw at the trade instant
    forward_usd, basis, fwd_source, basis_age_s
    trade_iv_ours, trade_iv_status   (from price_eth; validated against Deribit's iv)
    mark_iv, mark_iv_status          (from mark_price_eth; Surface A's input in Phase 3)
  strict (< t - latency): what a resting quote could have known
    forward_strict_usd, index_strict_usd, trade_iv_strict
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from mm.config import Config, load_config
from mm.data.db import connect
from mm.pricing.forwards import ForwardCurve, future_basis_obs, parity_basis_obs
from mm.pricing.iv import implied_vol

log = logging.getLogger(__name__)
CACHE_NAME = "option_trades_priced"


def index_series(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """One index series from every trade stream (each trade carries the index at its
    instant), so the latest index before any t is as fresh as the data allows."""
    s = pd.concat([f[["ts", "index_price_usd"]] for f in frames], ignore_index=True)
    return s.dropna().sort_values("ts", kind="stable").reset_index(drop=True)


def build_curve(cfg: Config, start=None, end=None) -> ForwardCurve:
    """Forward curve over [start, end) (default: the study period), plus a day of warm-up."""
    con = connect(cfg.data.clean_dir)
    s = str(start or cfg.study_period.start)
    e = str(end or cfg.study_period.end)
    # A day of warm-up before the start so the first trades have a basis.
    rng = f"ts >= timestamp '{s}' - interval 1 day and ts < timestamp '{e}'"
    index = con.execute(f"select ts, index_price_usd from perp_trades where {rng} order by ts").df()
    fut = con.execute(
        f"select ts, instrument_name, expiry, mark_price_usd, index_price_usd "
        f"from future_trades where {rng} order by ts"
    ).df()
    opt = con.execute(
        f"select ts, expiry, strike_usd, option_type, mark_price_eth, index_price_usd "
        f"from option_trades where {rng} order by ts"
    ).df()
    fcfg = cfg.pricing.forwards
    log.info("index %d, futures %d, option rows for parity %d", len(index), len(fut), len(opt))
    index = index_series([index, fut, opt])
    return ForwardCurve(index, future_basis_obs(fut), parity_basis_obs(opt, fcfg), fcfg)


def price_trades(trades: pd.DataFrame, curve: ForwardCurve, latency_ms: int) -> pd.DataFrame:
    out = trades.reset_index(drop=True).copy()
    k, t = out["strike_usd"].to_numpy(), out["t_years"].to_numpy()
    call = (out["option_type"] == "C").to_numpy()
    # inclusive: what Deribit saw at the trade instant
    fw = curve.forwards(out["ts"], out["expiry"], 0, inclusive=True, index=out["index_price_usd"])
    f = fw["forward_usd"].to_numpy()
    out["forward_usd"] = f
    out["basis"] = fw["basis"].to_numpy()
    out["fwd_source"] = fw["source"].to_numpy()
    out["basis_age_s"] = fw["basis_age_s"].to_numpy()
    iv, st = implied_vol(out["price_eth"].to_numpy(), f, k, t, call)
    out["trade_iv_ours"], out["trade_iv_status"] = iv, st
    iv, st = implied_vol(out["mark_price_eth"].to_numpy(), f, k, t, call)
    out["mark_iv"], out["mark_iv_status"] = iv, st
    # strict: what a quote resting before the trade (after latency) could have known
    fs = curve.forwards(out["ts"], out["expiry"], latency_ms)
    out["forward_strict_usd"] = fs["forward_usd"].to_numpy()
    out["index_strict_usd"] = fs["index_usd"].to_numpy()
    iv, _ = implied_vol(out["price_eth"].to_numpy(), out["forward_strict_usd"], k, t, call)
    out["trade_iv_strict"] = iv
    return out


def enrich(cfg: Config) -> Path:
    curve = build_curve(cfg)
    con = connect(cfg.data.clean_dir)
    out_dir = Path("data/cache") / CACHE_NAME
    days = con.execute("select distinct date from option_trades order by 1").fetchall()
    for (d,) in days:
        day = pd.Timestamp(d).date()
        trades = con.execute(
            f"select * exclude (date) from option_trades where date = '{day}' order by ts"
        ).df()
        priced = price_trades(trades, curve, cfg.sim.latency_ms)
        p = out_dir / f"date={day}" / "part.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        priced.to_parquet(p, index=False, compression="zstd")
        log.info("%s priced %d", day, len(priced))
    return out_dir


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="mm.pricing.enrich", description=__doc__)
    p.add_argument("--config", required=True)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(enrich(load_config(a.config)))


if __name__ == "__main__":
    main()
