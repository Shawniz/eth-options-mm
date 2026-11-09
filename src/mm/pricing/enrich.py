"""Price every clean option trade: forward, our trade IV and our mark IV.

    uv run python -m mm.pricing.enrich --config config/base.yaml

Reads data/clean, writes data/cache/option_trades_priced/date=YYYY-MM-DD/part.parquet
(strategy-independent work is cached; CLAUDE.md). Uses latency = config sim.latency_ms.

Added columns:
    forward_usd, basis, fwd_source, fwd_index_usd, basis_age_s
    trade_iv_ours, trade_iv_status   (from price_eth)
    mark_iv, mark_iv_status          (from mark_price_eth; Surface A's input in Phase 3)
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


def build_curve(cfg: Config) -> ForwardCurve:
    con = connect(cfg.data.clean_dir)
    s, e = str(cfg.study_period.start), str(cfg.study_period.end)
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
    return ForwardCurve(index, future_basis_obs(fut), parity_basis_obs(opt, fcfg), fcfg)


def price_trades(trades: pd.DataFrame, curve: ForwardCurve, latency_ms: int) -> pd.DataFrame:
    fw = curve.forwards(trades["ts"], trades["expiry"], latency_ms)
    out = trades.reset_index(drop=True).copy()
    out["forward_usd"] = fw["forward_usd"].to_numpy()
    out["basis"] = fw["basis"].to_numpy()
    out["fwd_source"] = fw["source"].to_numpy()
    out["fwd_index_usd"] = fw["index_usd"].to_numpy()
    out["basis_age_s"] = fw["basis_age_s"].to_numpy()
    f, k, t = out["forward_usd"], out["strike_usd"], out["t_years"]
    call = (out["option_type"] == "C").to_numpy()
    iv, st = implied_vol(out["price_eth"].to_numpy(), f, k, t, call)
    out["trade_iv_ours"], out["trade_iv_status"] = iv, st
    iv, st = implied_vol(out["mark_price_eth"].to_numpy(), f, k, t, call)
    out["mark_iv"], out["mark_iv_status"] = iv, st
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
