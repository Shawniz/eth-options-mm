"""Download raw Deribit history into data/raw/<source>/date=YYYY-MM-DD/part.parquet.

Raw files are written once and never overwritten. A day is fetched fully in memory
and written atomically (tmp file + rename), so a crash never leaves a partial day.

Sources:
    option_trades  history server, all ETH option trades (kind=option)
    future_trades  history server, all ETH futures incl. ETH-PERPETUAL (kind=future)
    dvol           main API, ETH DVOL candles (not available on the history server)
    funding        main API, hourly ETH-PERPETUAL funding records
Snapshots (whole history at fetch time, raw/<name>/fetched=YYYY-MM-DD/):
    instruments      download_instruments()
    delivery_prices  download_delivery_prices()
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from mm.config import Config

log = logging.getLogger(__name__)

TRADE_SOURCES = {"option_trades": "option", "future_trades": "future"}
SOURCES = (*TRADE_SOURCES, "dvol", "funding")
SNAPSHOTS = ("instruments", "delivery_prices")
# get_funding_rate_history silently keeps only the LAST 744 records of a longer range.
FUNDING_MAX_RECORDS = 744
DAY_MS = 86_400_000

# Typed columns we know about. Anything else the API sends goes into extra_json, so
# raw files stay lossless if Deribit adds fields.
TRADE_SCHEMA = pa.schema(
    [
        ("timestamp", pa.int64()),
        ("trade_id", pa.string()),
        ("trade_seq", pa.int64()),
        ("instrument_name", pa.string()),
        ("direction", pa.string()),
        ("price", pa.float64()),
        ("mark_price", pa.float64()),
        ("index_price", pa.float64()),
        ("iv", pa.float64()),
        ("amount", pa.float64()),
        ("contracts", pa.float64()),
        ("tick_direction", pa.int8()),
        ("liquidation", pa.string()),
        ("block_trade_id", pa.string()),
        ("block_trade_leg_count", pa.int32()),
        ("combo_id", pa.string()),
        ("combo_trade_id", pa.string()),
        ("extra_json", pa.string()),
    ]
)

DVOL_SCHEMA = pa.schema(
    [
        (c, pa.int64() if c == "timestamp" else pa.float64())
        for c in ("timestamp", "open", "high", "low", "close")
    ]
)

FUNDING_SCHEMA = pa.schema(
    [
        ("timestamp", pa.int64()),
        ("index_price", pa.float64()),
        ("prev_index_price", pa.float64()),
        ("interest_1h", pa.float64()),
        ("interest_8h", pa.float64()),
    ]
)

DELIVERY_SCHEMA = pa.schema([("date", pa.string()), ("delivery_price", pa.float64())])

INSTRUMENT_SCHEMA = pa.schema(
    [
        ("instrument_name", pa.string()),
        ("kind", pa.string()),
        ("expiration_timestamp", pa.int64()),
        ("creation_timestamp", pa.int64()),
        ("strike", pa.float64()),
        ("option_type", pa.string()),
        ("contract_size", pa.float64()),
        ("tick_size", pa.float64()),
        ("maker_commission", pa.float64()),
        ("taker_commission", pa.float64()),
        ("settlement_period", pa.string()),
        ("raw_json", pa.string()),
    ]
)


class Client(Protocol):
    def get(self, method: str, params: dict[str, Any]) -> Any: ...


# ---------- paths and IO ----------


def raw_path(raw_dir: Path, source: str, day: date) -> Path:
    return Path(raw_dir) / source / f"date={day.isoformat()}" / "part.parquet"


def write_raw(table: pa.Table, path: Path) -> Path:
    """Write once. Refuses to overwrite an existing raw file."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"raw file exists, refusing to overwrite: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)
    return path


def day_bounds_ms(day: date) -> tuple[int, int]:
    """Inclusive [start, end] in ms for a UTC day (the history API bounds are inclusive)."""
    start = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
    return start, start + DAY_MS - 1


# ---------- tables ----------


def trades_to_table(rows: list[dict]) -> pa.Table:
    known = set(TRADE_SCHEMA.names) - {"extra_json"}
    cols: dict[str, list] = {n: [] for n in TRADE_SCHEMA.names}
    for r in rows:
        for n in known:
            cols[n].append(r.get(n))
        extra = {k: v for k, v in r.items() if k not in known}
        cols["extra_json"].append(json.dumps(extra, sort_keys=True) if extra else None)
    return pa.table(cols, schema=TRADE_SCHEMA)


def _rows_to_table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    return pa.table({n: [r.get(n) for r in rows] for n in schema.names}, schema=schema)


def delivery_to_table(rows: list[dict]) -> pa.Table:
    return _rows_to_table(rows, DELIVERY_SCHEMA)


def instruments_to_table(rows: list[dict]) -> pa.Table:
    cols: dict[str, list] = {n: [] for n in INSTRUMENT_SCHEMA.names}
    for r in rows:
        for n in INSTRUMENT_SCHEMA.names[:-1]:
            cols[n].append(r.get(n))
        cols["raw_json"].append(json.dumps(r, sort_keys=True))
    return pa.table(cols, schema=INSTRUMENT_SCHEMA)


# ---------- fetching ----------


def fetch_trades(
    client: Client, method: str, params: dict, start_ms: int, end_ms: int, page_size: int
) -> list[dict]:
    """All trades with start_ms <= timestamp <= end_ms, deduped by trade_id, time-sorted.

    The API pages by timestamp only, so each next page starts at the last timestamp seen;
    trades on that boundary come back twice and are deduped. If a full page has a single
    timestamp we cannot advance and fail loudly rather than silently drop trades.
    """
    out: dict[str, dict] = {}
    t = start_ms
    while True:
        res = client.get(
            method,
            {
                **params,
                "start_timestamp": t,
                "end_timestamp": end_ms,
                "count": page_size,
                "sorting": "asc",
            },
        )
        trades = res["trades"]
        for tr in trades:
            if start_ms <= tr["timestamp"] <= end_ms:
                out[tr["trade_id"]] = tr
        if not res.get("has_more") or not trades:
            break
        nxt = trades[-1]["timestamp"]
        if nxt <= t:
            raise RuntimeError(
                f"{method}: cannot advance past ts={t}: >= {page_size} trades share one ms"
            )
        t = nxt

    def key(tr: dict) -> tuple:
        tid = tr["trade_id"].rsplit("-", 1)[-1]
        return (tr["timestamp"], int(tid) if tid.isdigit() else 0, tr["trade_id"])

    return sorted(out.values(), key=key)


def fetch_dvol(client: Client, currency: str, start_ms: int, end_ms: int, res_s: int) -> list:
    """DVOL candles [ts, o, h, l, c] (percent). The API pages backwards via `continuation`."""
    rows: dict[int, list] = {}
    end = end_ms
    while True:
        r = client.get(
            "public/get_volatility_index_data",
            {
                "currency": currency,
                "start_timestamp": start_ms,
                "end_timestamp": end,
                "resolution": res_s,
            },
        )
        for row in r["data"]:
            if start_ms <= row[0] <= end_ms:
                rows[row[0]] = row
        cont = r.get("continuation")
        if cont is None or not r["data"]:
            break
        if cont >= end:
            raise RuntimeError(f"DVOL continuation did not move back: {cont} >= {end}")
        end = cont
    return [rows[k] for k in sorted(rows)]


def fetch_funding(client: Client, instrument: str, start_ms: int, end_ms: int) -> list[dict]:
    """Hourly funding records with start_ms <= timestamp <= end_ms (one day = 24 rows).

    The API truncates long ranges to the most recent records without saying so, so a
    response that hits the cap is treated as an error rather than trusted.
    """
    rows = client.get(
        "public/get_funding_rate_history",
        {"instrument_name": instrument, "start_timestamp": start_ms, "end_timestamp": end_ms},
    )
    if len(rows) >= FUNDING_MAX_RECORDS:
        raise RuntimeError(f"funding response may be truncated ({len(rows)} records)")
    rows = [r for r in rows if start_ms <= r["timestamp"] <= end_ms]
    return sorted({r["timestamp"]: r for r in rows}.values(), key=lambda r: r["timestamp"])


def _check_day_complete(day: date) -> None:
    _, end = day_bounds_ms(day)
    if end >= time.time() * 1000:
        raise ValueError(f"{day} is not complete yet (UTC); refusing to cache a partial day")


def download_day(
    source: str, day: date, cfg: Config, *, history: Client | None, main: Client | None
) -> Path | None:
    """Fetch and cache one UTC day of `source`. Returns None if already cached."""
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}; expected one of {SOURCES}")
    _check_day_complete(day)
    path = raw_path(cfg.data.raw_dir, source, day)
    if path.exists():
        log.debug("skip %s %s (cached)", source, day)
        return None
    s, e = day_bounds_ms(day)
    if source in TRADE_SOURCES:
        rows = fetch_trades(
            history,
            "public/get_last_trades_by_currency_and_time",
            {"currency": cfg.data.currency, "kind": TRADE_SOURCES[source]},
            s,
            e,
            cfg.data.page_size,
        )
        table = trades_to_table(rows)
    elif source == "funding":
        rows = fetch_funding(main, cfg.specs.perp.instrument, s, e)
        table = _rows_to_table(rows, FUNDING_SCHEMA)
    else:
        rows = fetch_dvol(main, cfg.data.currency, s, e, cfg.data.dvol_resolution_s)
        table = pa.table(
            {n: [r[i] for r in rows] for i, n in enumerate(DVOL_SCHEMA.names)}, schema=DVOL_SCHEMA
        )
    write_raw(table, path)
    log.info("%s %s: %d rows", source, day, table.num_rows)
    return path


def download_instruments(cfg: Config, history: Client, fetched: date | None = None) -> list[Path]:
    """Snapshot of option and future specs (active + expired) from the history server.

    Written to raw/instruments/fetched=YYYY-MM-DD/<kind>.parquet;
    a re-run on the same day is a no-op.
    """
    fetched = fetched or datetime.now(UTC).date()
    out = []
    for kind in ("option", "future"):
        path = Path(cfg.data.raw_dir) / "instruments" / f"fetched={fetched}" / f"{kind}.parquet"
        if path.exists():
            continue
        rows: dict[str, dict] = {}
        for expired in (False, True):
            for r in history.get(
                "public/get_instruments",
                {"currency": cfg.data.currency, "kind": kind, "expired": expired},
            ):
                rows[r["instrument_name"]] = r
        out.append(write_raw(instruments_to_table(list(rows.values())), path))
        log.info("instruments %s: %d", kind, len(rows))
    return out


def download_delivery_prices(
    cfg: Config,
    main: Client,
    fetched: date | None = None,
    index_name: str | None = None,
    page_size: int = 100,
) -> list[Path]:
    """Snapshot of all official daily delivery (settlement) prices for the index.

    Written to raw/delivery_prices/fetched=YYYY-MM-DD/<index>.parquet; same-day re-run is a no-op.
    """
    fetched = fetched or datetime.now(UTC).date()
    index_name = index_name or f"{cfg.data.currency.lower()}_usd"
    path = (
        Path(cfg.data.raw_dir) / "delivery_prices" / f"fetched={fetched}" / f"{index_name}.parquet"
    )
    if path.exists():
        return []
    rows: dict[str, dict] = {}
    offset, total = 0, None
    while total is None or offset < total:
        r = main.get(
            "public/get_delivery_prices",
            {"index_name": index_name, "offset": offset, "count": page_size},
        )
        total = r["records_total"]
        if not r["data"]:
            break
        for x in r["data"]:
            rows[x["date"]] = x
        offset += page_size
    if len(rows) != total:
        raise RuntimeError(f"delivery prices: got {len(rows)} of {total} records")
    log.info("delivery prices %s: %d", index_name, len(rows))
    return [write_raw(delivery_to_table(sorted(rows.values(), key=lambda x: x["date"])), path)]


def iter_days(start: date, end: date):
    """UTC days in [start, end)."""
    d = start
    while d < end:
        yield d
        d += timedelta(days=1)
