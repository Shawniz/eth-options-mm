import json
from datetime import UTC, date, datetime

import pandas as pd
import pytest
from conftest import make_trade

from mm.data.clean import (
    check_trades_against_instruments,
    clean_day,
    clean_instruments,
    clean_path,
)
from mm.data.download import (
    day_bounds_ms,
    instruments_to_table,
    raw_path,
    trades_to_table,
    write_raw,
)

DAY = date(2025, 1, 1)
S, _ = day_bounds_ms(DAY)


def _write_options(cfg, rows):
    write_raw(trades_to_table(rows), raw_path(cfg.data.raw_dir, "option_trades", DAY))


def _option_rows():
    t = make_trade(S + 5)
    return [
        make_trade(S + 1, iv=59.72, price=0.0135, mark_price=0.013629, index_price=3329.67),
        make_trade(S + 2, block_trade_id="BLOCK-1", block_trade_leg_count=1),
        make_trade(S + 3, combo_id="ETH-CS-1", combo_trade_id="c1"),
        make_trade(S + 4, block_trade_id="BLOCK-2", combo_id="ETH-CS-2"),  # counted once
        make_trade(S + 6, liquidation="T"),  # kept
        make_trade(S + 7, instrument="ETH-BADNAME"),
        t,
        dict(t),  # duplicate trade_id
    ]


def test_option_clean_filters_and_reconciles(cfg):
    _write_options(cfg, _option_rows())
    rep = clean_day("option_trades", DAY, cfg)
    assert rep["raw_rows"] == 8
    assert rep["dropped"] == {"duplicate": 1, "block": 2, "combo": 1, "bad_instrument": 1}
    assert rep["kept"] == 3
    assert rep["raw_rows"] == rep["kept"] + sum(rep["dropped"].values())
    df = pd.read_parquet(clean_path(cfg.data.clean_dir, "option_trades", DAY))
    assert len(df) == 3
    report_file = cfg.data.clean_dir / "_reports" / "option_trades" / "2025-01-01.json"
    assert json.loads(report_file.read_text()) == rep


def test_option_clean_units_and_columns(cfg):
    _write_options(cfg, _option_rows())
    clean_day("option_trades", DAY, cfg)
    df = pd.read_parquet(clean_path(cfg.data.clean_dir, "option_trades", DAY))
    r = df.iloc[0]
    assert str(df["ts"].dtype) == "datetime64[ms, UTC]"
    assert r["ts"] == pd.Timestamp(S + 1, unit="ms", tz="UTC")
    assert r["iv"] == pytest.approx(0.5972)  # percent -> decimal
    assert r["price_eth"] == 0.0135
    assert r["mark_price_eth"] == 0.013629
    assert r["index_price_usd"] == 3329.67
    assert r["strike_usd"] == 4500.0 and r["option_type"] == "C"
    assert r["expiry"] == pd.Timestamp(datetime(2025, 2, 28, 8, tzinfo=UTC))
    assert r["direction"] in ("buy", "sell")
    assert "amount_contracts" in df.columns
    assert df["ts"].is_monotonic_increasing
    assert df["is_liquidation"].tolist() == [False, False, True]  # S+1, S+5, S+6(liq)
    assert not {"price", "iv_pct", "block_trade_id", "combo_id"} & set(df.columns)


def test_flags_can_keep_block_and_combo(cfg):
    d = cfg.data.model_copy(update={"exclude_block_trades": False, "exclude_combo_trades": False})
    cfg = cfg.model_copy(update={"data": d})
    _write_options(cfg, _option_rows())
    rep = clean_day("option_trades", DAY, cfg)
    assert rep["dropped"] == {"duplicate": 1, "block": 0, "combo": 0, "bad_instrument": 1}


def test_future_clean_splits_perp_and_dated(cfg):
    rows = [
        make_trade(
            S + 1, instrument="ETH-PERPETUAL", price=3331.1, amount=9140.0, contracts=9140.0
        ),
        make_trade(S + 2, instrument="ETH-27JUN25", price=3513.25, amount=226.0, contracts=226.0),
        make_trade(S + 3, instrument="ETH-PERPETUAL", block_trade_id="BLOCK-9"),
    ]
    for r in rows:
        r.pop("iv")
    write_raw(trades_to_table(rows), raw_path(cfg.data.raw_dir, "future_trades", DAY))
    rep = clean_day("future_trades", DAY, cfg)
    assert rep["kept"] == 2 and rep["dropped"]["block"] == 1
    assert rep["kept_by_output"] == {"perp_trades": 1, "future_trades": 1}
    perp = pd.read_parquet(clean_path(cfg.data.clean_dir, "perp_trades", DAY))
    fut = pd.read_parquet(clean_path(cfg.data.clean_dir, "future_trades", DAY))
    assert perp["price_usd"].tolist() == [3331.1]
    assert perp["amount_usd"].tolist() == [9140.0]  # reversed contract: USD notional
    assert fut["expiry"].iloc[0] == pd.Timestamp(datetime(2025, 6, 27, 8, tzinfo=UTC))
    assert "iv" not in perp.columns


def test_dvol_clean_decimal_and_available_at(cfg):
    import pyarrow as pa

    tbl = pa.table(
        {
            "timestamp": [S, S + 3_600_000],
            "open": [70.35, 70.46],
            "high": [70.58, 70.46],
            "low": [70.34, 70.25],
            "close": [70.46, 70.40],
        }
    )
    write_raw(tbl, raw_path(cfg.data.raw_dir, "dvol", DAY))
    clean_day("dvol", DAY, cfg)
    df = pd.read_parquet(clean_path(cfg.data.clean_dir, "dvol", DAY))
    assert df["dvol_close"].iloc[0] == pytest.approx(0.7046)
    # A bar's close is only known when the bar ends: guards against lookahead.
    res = cfg.data.dvol_resolution_s
    assert (df["available_at"] - df["ts"]).dt.total_seconds().tolist() == [res, res]


def test_clean_overwrite_is_idempotent(cfg):
    _write_options(cfg, _option_rows())
    a = clean_day("option_trades", DAY, cfg)
    b = clean_day("option_trades", DAY, cfg)
    assert a == b


def _instrument(name, expiry_ms, strike=None, option_type=None, contract_size=1.0, kind="option"):
    r = {
        "instrument_name": name,
        "kind": kind,
        "expiration_timestamp": expiry_ms,
        "creation_timestamp": expiry_ms - 86_400_000 * 30,
        "contract_size": contract_size,
        "tick_size": 0.0005,
        "maker_commission": 0.0003,
        "taker_commission": 0.0003,
        "settlement_period": "month",
        "tick_size_steps": [],
    }
    if strike is not None:
        r["strike"] = strike
        r["option_type"] = option_type
    return r


FEB28 = int(datetime(2025, 2, 28, 8, tzinfo=UTC).timestamp() * 1000)


def _write_instruments(cfg, rows, kind="option"):
    p = cfg.data.raw_dir / "instruments" / "fetched=2026-11-05" / f"{kind}.parquet"
    write_raw(instruments_to_table(rows), p)


def test_clean_instruments_validates_parser(cfg):
    _write_instruments(cfg, [_instrument("ETH-28FEB25-4500-C", FEB28, 4500.0, "call")])
    df = clean_instruments(cfg)["option"]
    assert df["strike_usd"].tolist() == [4500.0]
    assert df["expiry"].iloc[0] == pd.Timestamp(FEB28, unit="ms", tz="UTC")


def test_clean_instruments_rejects_mismatch(cfg):
    _write_instruments(cfg, [_instrument("ETH-28FEB25-4500-C", FEB28 + 1, 4500.0, "call")])
    with pytest.raises(ValueError, match="expiry"):
        clean_instruments(cfg)


def test_clean_instruments_rejects_non_unit_contract(cfg):
    _write_instruments(cfg, [_instrument("ETH-28FEB25-4500-C", FEB28, 4500.0, "call", 10.0)])
    with pytest.raises(ValueError, match="contract_size"):
        clean_instruments(cfg)


def test_trades_checked_against_instrument_list(cfg):
    _write_instruments(cfg, [_instrument("ETH-28FEB25-4500-C", FEB28, 4500.0, "call")])
    clean_instruments(cfg)
    _write_options(cfg, [make_trade(S + 1), make_trade(S + 2, instrument="ETH-28FEB25-5000-C")])
    clean_day("option_trades", DAY, cfg)
    missing = check_trades_against_instruments(cfg)
    assert missing == ["ETH-28FEB25-5000-C"]


def test_clean_instruments_handles_perpetual(cfg):
    jun27 = int(datetime(2025, 6, 27, 8, tzinfo=UTC).timestamp() * 1000)
    rows = [
        _instrument("ETH-PERPETUAL", 32503708800000, kind="future"),
        _instrument("ETH-27JUN25", jun27, kind="future"),
    ]
    _write_instruments(cfg, rows, kind="future")
    df = clean_instruments(cfg)["future"].set_index("instrument_name")
    assert df.loc["ETH-PERPETUAL", "kind"] == "perpetual"
    assert pd.isna(df.loc["ETH-PERPETUAL", "expiry"])
    assert df.loc["ETH-27JUN25", "expiry"] == pd.Timestamp(jun27, unit="ms", tz="UTC")
