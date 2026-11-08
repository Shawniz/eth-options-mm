"""Raw -> clean. Reads data/raw (never modified), writes data/clean.

Conventions applied here (see CLAUDE.md "Units and conventions"):
- `ts`: timezone-aware UTC (ms resolution); raw integer ms is dropped.
- Price columns carry their currency: option premiums `*_eth`, strikes/index/futures `*_usd`.
- `iv` is decimal (raw API sends percent).
- Option size `amount_contracts` (1 contract = 1 ETH; checked against instrument specs).
  ETH futures/perp are reversed contracts: `amount` is USD notional -> `amount_usd`.
- `direction` is the taker (aggressor) side: 'buy' lifted an ask, 'sell' hit a bid.

Filters, applied in order; each dropped row is counted under the first filter it fails,
so raw_rows == kept + sum(dropped) always holds:
    duplicate (trade_id), block (block_trade_id), combo (combo_id), bad_instrument,
    invalid (cannot be a real print: price <= 0, size <= 0, missing mark/index,
             or an option traded at/after its expiry).
Suspect option prints are KEPT and flagged (is_suspect, suspect_reason; decision D013):
    iv_nonpositive, iv_high (> data.suspect.iv_max), far_from_mark.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd

from mm.config import Config
from mm.data.download import raw_path
from mm.data.instruments import EXPIRY_HOUR_UTC, parse_instrument
from mm.pricing.time import year_frac

log = logging.getLogger(__name__)

CLEAN_SOURCES = ("option_trades", "future_trades", "dvol", "funding")


def clean_path(clean_dir: Path, dataset: str, day: date) -> Path:
    return Path(clean_dir) / dataset / f"date={day.isoformat()}" / "part.parquet"


def _ts(ms: pd.Series) -> pd.Series:
    return pd.to_datetime(ms.astype("int64"), unit="ms", utc=True).astype("datetime64[ms, UTC]")


def _write(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)


def _parse_names(names: pd.Series) -> pd.DataFrame:
    parsed = {}
    for n in names.unique():
        try:
            parsed[n] = parse_instrument(n)
        except ValueError:
            parsed[n] = None
    return pd.DataFrame(
        {
            "ok": names.map(lambda n: parsed[n] is not None),
            "kind": names.map(lambda n: parsed[n].kind if parsed[n] else None),
            "expiry": names.map(lambda n: parsed[n].expiry if parsed[n] else None),
            "strike_usd": names.map(lambda n: parsed[n].strike_usd if parsed[n] else None),
            "option_type": names.map(lambda n: parsed[n].option_type if parsed[n] else None),
        },
        index=names.index,
    )


def _filter_trades(raw: pd.DataFrame, cfg: Config, expect_kinds: set[str]):
    dropped = {"duplicate": 0, "block": 0, "combo": 0, "bad_instrument": 0}
    df = raw
    dup = df.duplicated("trade_id", keep="first")
    dropped["duplicate"] = int(dup.sum())
    df = df[~dup]
    if cfg.data.exclude_block_trades:
        m = df["block_trade_id"].notna()
        dropped["block"] = int(m.sum())
        df = df[~m]
    if cfg.data.exclude_combo_trades:
        m = df["combo_id"].notna()
        dropped["combo"] = int(m.sum())
        df = df[~m]
    p = _parse_names(df["instrument_name"])
    bad = ~p["ok"] | ~p["kind"].isin(expect_kinds)
    dropped["bad_instrument"] = int(bad.sum())
    if bad.any():
        log.warning("unparseable instruments: %s", sorted(df.loc[bad, "instrument_name"].unique()))
    df, p = df[~bad], p[~bad]
    invalid = (
        ~(df["price"] > 0)
        | ~(df["amount"] > 0)
        | df["index_price"].isna()
        | df["mark_price"].isna()
    )
    if "option" in expect_kinds:
        exp_ms = p["expiry"].map(lambda d: int(d.timestamp() * 1000))
        invalid |= df["timestamp"] >= exp_ms
    dropped["invalid"] = int(invalid.sum())
    return df[~invalid].copy(), p[~invalid], dropped


def _suspect(out: pd.DataFrame, cfg: Config) -> pd.Series:
    """Reason a kept option print is suspect, or None. First matching reason wins."""
    sc, spec = cfg.data.suspect, cfg.specs.option
    tick = out["mark_price_eth"].map(spec.tick_for)
    dev = (out["price_eth"] - out["mark_price_eth"]).abs()
    far = (dev > sc.mark_rel_dev * out["mark_price_eth"]) & (dev > sc.mark_min_ticks * tick)
    reason = pd.Series(pd.NA, index=out.index, dtype="string")
    reason[far] = "far_from_mark"
    reason[out["iv"] > sc.iv_max] = "iv_high"
    reason[~(out["iv"] > 0)] = "iv_nonpositive"
    return reason


def _base_cols(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts": _ts(df["timestamp"]),
            "trade_id": df["trade_id"],
            "trade_seq": df["trade_seq"].astype("int64"),
            "instrument_name": df["instrument_name"],
            "direction": df["direction"],
        }
    )


def _clean_options(raw: pd.DataFrame, cfg: Config) -> tuple[dict[str, pd.DataFrame], dict]:
    df, p, dropped = _filter_trades(raw, cfg, {"option"})
    out = _base_cols(df)
    out["expiry"] = pd.to_datetime(p["expiry"], utc=True).astype("datetime64[ms, UTC]")
    out["strike_usd"] = p["strike_usd"].astype("float64")
    out["option_type"] = p["option_type"]
    out["price_eth"] = df["price"].astype("float64")
    out["mark_price_eth"] = df["mark_price"].astype("float64")
    out["iv"] = df["iv"].astype("float64") / 100.0
    out["index_price_usd"] = df["index_price"].astype("float64")
    out["amount_contracts"] = df["amount"].astype("float64")
    out["is_liquidation"] = df["liquidation"].notna()
    out["t_years"] = year_frac(out["ts"], out["expiry"])
    out["suspect_reason"] = _suspect(out, cfg)
    out["is_suspect"] = out["suspect_reason"].notna()
    return {"option_trades": out}, dropped


def _clean_futures(raw: pd.DataFrame, cfg: Config) -> tuple[dict[str, pd.DataFrame], dict]:
    df, p, dropped = _filter_trades(raw, cfg, {"future", "perpetual"})
    out = _base_cols(df)
    out["expiry"] = pd.to_datetime(p["expiry"], utc=True).astype("datetime64[ms, UTC]")
    out["price_usd"] = df["price"].astype("float64")
    out["mark_price_usd"] = df["mark_price"].astype("float64")
    out["index_price_usd"] = df["index_price"].astype("float64")
    out["amount_usd"] = df["amount"].astype("float64")
    out["is_liquidation"] = df["liquidation"].notna()
    is_perp = (p["kind"] == "perpetual").to_numpy()
    return {
        "perp_trades": out[is_perp].drop(columns="expiry"),
        "future_trades": out[~is_perp],
    }, dropped


def _clean_dvol(raw: pd.DataFrame, cfg: Config) -> tuple[dict[str, pd.DataFrame], dict]:
    out = pd.DataFrame({"ts": _ts(raw["timestamp"])})
    # A candle is stamped at its open; its close is only knowable when it ends.
    out["available_at"] = out["ts"] + pd.Timedelta(seconds=cfg.data.dvol_resolution_s)
    for c in ("open", "high", "low", "close"):
        out[f"dvol_{c}"] = raw[c].astype("float64") / 100.0
    return {"dvol": out}, {}


def _clean_funding(raw: pd.DataFrame, cfg: Config) -> tuple[dict[str, pd.DataFrame], dict]:
    out = pd.DataFrame({"ts": _ts(raw["timestamp"])})
    # Each record covers the hour ENDING at ts, so it is known from ts onwards.
    out["available_at"] = out["ts"]
    out["interest_1h"] = raw["interest_1h"].astype("float64")  # fraction, not percent
    out["interest_8h"] = raw["interest_8h"].astype("float64")
    out["index_price_usd"] = raw["index_price"].astype("float64")
    out["prev_index_price_usd"] = raw["prev_index_price"].astype("float64")
    return {"funding": out}, {}


_CLEANERS = {
    "option_trades": _clean_options,
    "future_trades": _clean_futures,
    "dvol": _clean_dvol,
    "funding": _clean_funding,
}


def clean_day(source: str, day: date, cfg: Config) -> dict:
    """Clean one raw day. Overwrites clean outputs (they are derived). Returns the report."""
    raw = pd.read_parquet(raw_path(cfg.data.raw_dir, source, day))
    outputs, dropped = _CLEANERS[source](raw, cfg)
    kept_by_output = {}
    for name, df in outputs.items():
        df = df.sort_values(["ts", "trade_seq"] if "trade_seq" in df else "ts", kind="stable")
        _write(df.reset_index(drop=True), clean_path(cfg.data.clean_dir, name, day))
        kept_by_output[name] = len(df)
    kept = sum(kept_by_output.values())
    report = {
        "source": source,
        "date": day.isoformat(),
        "raw_rows": len(raw),
        "kept": kept,
        "dropped": dropped,
    }
    if len(outputs) > 1:
        report["kept_by_output"] = kept_by_output
    if source == "option_trades":
        reasons = outputs["option_trades"]["suspect_reason"].value_counts()
        report["suspect"] = {k: int(v) for k, v in reasons.items()}
    if report["raw_rows"] != kept + sum(dropped.values()):
        raise AssertionError(f"row counts do not reconcile: {report}")
    rp = Path(cfg.data.clean_dir) / "_reports" / source / f"{day.isoformat()}.json"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps(report, indent=2))
    return report


# ---------- instruments ----------


def clean_instruments(cfg: Config) -> dict[str, pd.DataFrame]:
    """Merge instrument snapshots (latest fetch wins), cross-check the name parser
    against Deribit's own expiry/strike/type fields, and require 1-ETH option contracts.
    Writes clean/instruments/<kind>.parquet."""
    files = sorted((Path(cfg.data.raw_dir) / "instruments").glob("fetched=*/*.parquet"))
    if not files:
        raise FileNotFoundError("no instrument snapshots; run download --sources instruments")
    raw = pd.concat(
        [pd.read_parquet(f).assign(_fetched=f.parent.name) for f in files], ignore_index=True
    )
    raw = raw.sort_values("_fetched").drop_duplicates("instrument_name", keep="last")
    out = {}
    for kind, g in raw.groupby("kind"):
        if kind not in ("option", "future"):
            continue
        p = _parse_names(g["instrument_name"])
        bad = g.loc[~p["ok"], "instrument_name"].tolist()
        if bad:
            log.warning("skipping unparseable %s instruments: %s", kind, bad[:10])
        g, p = g[p["ok"]], p[p["ok"]]
        dated = p["expiry"].notna()
        exp_ms = p["expiry"].map(lambda d: -1 if pd.isna(d) else int(d.timestamp() * 1000))
        mism = g.loc[dated & (exp_ms != g["expiration_timestamp"])]
        if len(mism):
            raise ValueError(
                f"expiry mismatch parser vs Deribit: {mism['instrument_name'].tolist()[:10]}"
            )
        df = pd.DataFrame(
            {
                "instrument_name": g["instrument_name"],
                "kind": p["kind"],
                "expiry": pd.to_datetime(g["expiration_timestamp"], unit="ms", utc=True)
                .where(dated)
                .astype("datetime64[ms, UTC]"),
                "created": pd.to_datetime(g["creation_timestamp"], unit="ms", utc=True).astype(
                    "datetime64[ms, UTC]"
                ),
                "contract_size": g["contract_size"].astype("float64"),
                "tick_size": g["tick_size"].astype("float64"),
                "maker_commission": g["maker_commission"].astype("float64"),
                "taker_commission": g["taker_commission"].astype("float64"),
                "settlement_period": g["settlement_period"],
                "min_trade_amount": g["raw_json"]
                .map(lambda j: json.loads(j).get("min_trade_amount"))
                .astype("float64"),
                "tick_size_steps": g["raw_json"].map(
                    lambda j: json.dumps(json.loads(j).get("tick_size_steps") or [], sort_keys=True)
                ),
            }
        )
        if kind == "option":
            api_type = g["option_type"].map({"call": "C", "put": "P"})
            if (api_type != p["option_type"]).any() or (g["strike"] != p["strike_usd"]).any():
                bad = g.loc[(api_type != p["option_type"]) | (g["strike"] != p["strike_usd"])]
                raise ValueError(f"strike/type mismatch: {bad['instrument_name'].tolist()[:10]}")
            if (g["contract_size"] != 1.0).any():
                bad = g.loc[g["contract_size"] != 1.0, "instrument_name"].tolist()
                raise ValueError(f"option contract_size != 1 ETH: {bad[:10]}")
            df["strike_usd"] = p["strike_usd"].astype("float64")
            df["option_type"] = p["option_type"]
        df = df.sort_values("instrument_name").reset_index(drop=True)
        path = Path(cfg.data.clean_dir) / "instruments" / f"{kind}.parquet"
        _write(df, path)
        out[kind] = df
    return out


def check_trades_against_instruments(cfg: Config) -> list[str]:
    """Traded instrument names missing from the clean instrument list (should be empty)."""
    from mm.data.db import connect

    con = connect(cfg.data.clean_dir)
    pairs = [
        ("option_trades", "instruments_option"),
        ("future_trades", "instruments_future"),
        ("perp_trades", "instruments_future"),
    ]
    views = {r[0] for r in con.execute("select view_name from duckdb_views()").fetchall()}
    missing: set[str] = set()
    for trades, inst in pairs:
        if trades in views and inst in views:
            q = (
                f"select distinct instrument_name from {trades} "
                f"anti join {inst} using (instrument_name)"
            )
            missing |= {r[0] for r in con.execute(q).fetchall()}
    return sorted(missing)


# ---------- delivery prices ----------


def clean_delivery_prices(cfg: Config) -> pd.DataFrame:
    """Latest snapshot -> clean/delivery_prices.parquet (date, settle_ts, delivery_price_usd).
    settle_ts is the 08:00 UTC expiry time the price settles."""
    files = sorted((Path(cfg.data.raw_dir) / "delivery_prices").glob("fetched=*/*.parquet"))
    if not files:
        raise FileNotFoundError(
            "no delivery price snapshot; run download --sources delivery_prices"
        )
    raw = pd.read_parquet(files[-1])
    d = pd.to_datetime(raw["date"], utc=True)
    df = pd.DataFrame(
        {
            "date": raw["date"],
            "settle_ts": (d + pd.Timedelta(hours=EXPIRY_HOUR_UTC)).astype("datetime64[ms, UTC]"),
            "delivery_price_usd": raw["delivery_price"].astype("float64"),
        }
    ).sort_values("date", ignore_index=True)
    _write(df, Path(cfg.data.clean_dir) / "delivery_prices.parquet")
    return df


def check_delivery_coverage(cfg: Config) -> list[str]:
    """Expiry dates of traded options (already expired) with no official delivery price."""
    from mm.data.db import connect

    con = connect(cfg.data.clean_dir)
    q = """
        select distinct strftime(expiry, '%Y-%m-%d') d from option_trades
        where expiry < now()
        and strftime(expiry, '%Y-%m-%d') not in (select date from delivery_prices)
        order by d
    """
    return [r[0] for r in con.execute(q).fetchall()]


# ---------- specs in config vs Deribit records ----------


def check_specs(cfg: Config) -> list[str]:
    """Compare config `specs` with Deribit's records for instruments alive during the
    study period. Returns a list of problems (empty = consistent)."""
    from mm.data.db import connect

    con = connect(cfg.data.clean_dir)
    start, end = str(cfg.study_period.start), str(cfg.study_period.end)
    alive = f"created < '{end}' and (expiry is null or expiry > '{start}')"
    o, p = cfg.specs.option, cfg.specs.perp
    steps = json.dumps(
        [{"above_price": s.above_price_eth, "tick_size": s.tick_eth} for s in o.tick_steps[1:]],
        sort_keys=True,
    )
    problems = []
    checks = [
        ("option", "contract_size", o.contract_size_eth),
        ("option", "min_trade_amount", o.min_order_contracts),
        ("option", "tick_size", o.tick_steps[0].tick_eth),
        ("option", "maker_commission", o.maker_fee_eth_per_contract),
        ("option", "taker_commission", o.taker_fee_eth_per_contract),
    ]
    views = {r[0] for r in con.execute("select view_name from duckdb_views()").fetchall()}
    for kind, col, want in checks:
        if f"instruments_{kind}" not in views:
            continue
        q = f"select {col}, count(*) from instruments_{kind} where {alive} group by 1"
        for val, n in con.execute(q).fetchall():
            if val is None or abs(val - want) > 1e-12:
                label = col.replace("_commission", " fee")
                problems.append(f"{kind} {label}: config {want}, Deribit {val} on {n} instruments")
    if "instruments_option" in views:
        q = f"select tick_size_steps, count(*) from instruments_option where {alive} group by 1"
        for val, n in con.execute(q).fetchall():
            if val != steps:
                problems.append(f"option tick steps: config {steps}, Deribit {val} on {n}")
    if "instruments_future" in views:
        row = con.execute(
            "select contract_size, min_trade_amount, tick_size from instruments_future "
            f"where instrument_name = '{p.instrument}'"
        ).fetchone()
        if row is None:
            problems.append(f"{p.instrument} not in instrument list")
        elif row != (want := (p.contract_size_usd, p.min_order_usd, p.tick_usd)):
            problems.append(f"perp specs: config {want}, Deribit {row}")
        # The perp record shows today's fees; dated futures alive in the study period carry
        # the study-period taker fee, which the perp shared.
        q = (
            "select taker_commission, count(*) from instruments_future "
            f"where kind = 'future' and {alive} group by 1"
        )
        for val, n in con.execute(q).fetchall():
            if abs(val - p.taker_fee_rate) > 1e-12:
                problems.append(
                    f"future taker fee: config perp {p.taker_fee_rate}, Deribit {val} on {n}"
                )
    return problems
