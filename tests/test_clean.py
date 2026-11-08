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
    assert rep["dropped"] == {
        "duplicate": 1,
        "block": 2,
        "combo": 1,
        "bad_instrument": 1,
        "invalid": 0,
    }
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
    assert rep["dropped"] == {
        "duplicate": 1,
        "block": 0,
        "combo": 0,
        "bad_instrument": 1,
        "invalid": 0,
    }


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


# ---------- bad prints (D013) ----------


def test_invalid_rows_dropped_and_counted(cfg):
    rows = [
        make_trade(S + 1),
        make_trade(S + 2, price=0.0),
        make_trade(S + 3, amount=0.0, contracts=0.0),
        make_trade(S + 4, mark_price=None),
        make_trade(S + 5, index_price=None),
        make_trade(S + 6, instrument="ETH-1JAN25-3300-C"),  # expired at 08:00, trade at 00:00 ok
    ]
    after_expiry = make_trade(S + 9 * 3_600_000, instrument="ETH-1JAN25-3300-C")
    _write_options(cfg, rows + [after_expiry])
    rep = clean_day("option_trades", DAY, cfg)
    assert rep["dropped"]["invalid"] == 5
    assert rep["kept"] == 2
    assert rep["raw_rows"] == rep["kept"] + sum(rep["dropped"].values())


def test_suspect_rows_tagged_not_dropped(cfg):
    rows = [
        make_trade(S + 1),  # normal
        make_trade(S + 2, iv=0.0),
        make_trade(S + 3, iv=400.0),
        make_trade(S + 4, price=0.0600, mark_price=0.0300),  # 100% and 60 ticks away
        make_trade(S + 5, price=0.0003, mark_price=0.0001),  # 200% but only 2 ticks away
    ]
    _write_options(cfg, rows)
    rep = clean_day("option_trades", DAY, cfg)
    assert rep["kept"] == 5
    assert rep["suspect"] == {"iv_nonpositive": 1, "iv_high": 1, "far_from_mark": 1}
    df = pd.read_parquet(clean_path(cfg.data.clean_dir, "option_trades", DAY))
    assert df["is_suspect"].tolist() == [False, True, True, True, False]
    reasons = df["suspect_reason"].astype("object").where(df["suspect_reason"].notna(), None)
    assert reasons.tolist() == [None, "iv_nonpositive", "iv_high", "far_from_mark", None]


def test_option_clean_has_t_years(cfg):
    _write_options(cfg, [make_trade(S + 1)])
    clean_day("option_trades", DAY, cfg)
    df = pd.read_parquet(clean_path(cfg.data.clean_dir, "option_trades", DAY))
    feb28 = datetime(2025, 2, 28, 8, tzinfo=UTC).timestamp() * 1000
    assert df["t_years"].iloc[0] == pytest.approx((feb28 - (S + 1)) / (365 * 86_400_000))


def test_future_invalid_dropped(cfg):
    rows = [
        make_trade(S + 1, instrument="ETH-PERPETUAL", price=3300.0),
        make_trade(S + 2, instrument="ETH-PERPETUAL", price=0.0),
        make_trade(S + 3, instrument="ETH-PERPETUAL", amount=0.0),
    ]
    for r in rows:
        r.pop("iv")
    write_raw(trades_to_table(rows), raw_path(cfg.data.raw_dir, "future_trades", DAY))
    rep = clean_day("future_trades", DAY, cfg)
    assert rep["dropped"]["invalid"] == 2 and rep["kept"] == 1


# ---------- funding and delivery ----------


def test_funding_clean(cfg):
    import pyarrow as pa

    from mm.data.download import FUNDING_SCHEMA

    tbl = pa.table(
        {
            "timestamp": [S, S + 3_600_000],
            "index_price": [3330.0, 3340.0],
            "prev_index_price": [3320.0, 3330.0],
            "interest_1h": [1e-6, -2e-6],
            "interest_8h": [8e-6, -1.6e-5],
        },
        schema=FUNDING_SCHEMA,
    )
    write_raw(tbl, raw_path(cfg.data.raw_dir, "funding", DAY))
    clean_day("funding", DAY, cfg)
    df = pd.read_parquet(clean_path(cfg.data.clean_dir, "funding", DAY))
    assert df["interest_1h"].tolist() == [1e-6, -2e-6]  # already a fraction, not percent
    assert (df["available_at"] == df["ts"]).all()  # record covers the hour ending at ts
    assert df["index_price_usd"].tolist() == [3330.0, 3340.0]


def test_delivery_prices_clean(cfg):
    from mm.data.clean import clean_delivery_prices
    from mm.data.download import delivery_to_table

    p = cfg.data.raw_dir / "delivery_prices" / "fetched=2026-11-08" / "eth_usd.parquet"
    write_raw(delivery_to_table([{"date": "2025-02-28", "delivery_price": 2237.5}]), p)
    df = clean_delivery_prices(cfg)
    assert df["settle_ts"].iloc[0] == pd.Timestamp(datetime(2025, 2, 28, 8, tzinfo=UTC))
    assert df["delivery_price_usd"].iloc[0] == 2237.5


def test_missing_delivery_prices_reported(cfg):
    from mm.data.clean import check_delivery_coverage, clean_delivery_prices
    from mm.data.download import delivery_to_table

    p = cfg.data.raw_dir / "delivery_prices" / "fetched=2026-11-08" / "eth_usd.parquet"
    write_raw(delivery_to_table([{"date": "2025-02-28", "delivery_price": 2237.5}]), p)
    clean_delivery_prices(cfg)
    _write_options(cfg, [make_trade(S + 1), make_trade(S + 2, instrument="ETH-7MAR25-3000-P")])
    clean_day("option_trades", DAY, cfg)
    assert check_delivery_coverage(cfg) == ["2025-03-07"]


# ---------- specs in config vs Deribit records ----------


def test_specs_check_passes_on_matching_records(cfg):
    from mm.data.clean import check_specs

    r = _instrument("ETH-28FEB25-4500-C", FEB28, 4500.0, "call")
    r.update(
        tick_size=0.0001,
        min_trade_amount=1.0,
        tick_size_steps=[{"above_price": 0.005, "tick_size": 0.0005}],
    )
    _write_instruments(cfg, [r])
    clean_instruments(cfg)
    assert check_specs(cfg) == []


def test_specs_check_flags_fee_mismatch(cfg):
    from mm.data.clean import check_specs

    r = _instrument("ETH-28FEB25-4500-C", FEB28, 4500.0, "call")
    r.update(
        tick_size=0.0001,
        min_trade_amount=1.0,
        maker_commission=0.0004,
        tick_size_steps=[{"above_price": 0.005, "tick_size": 0.0005}],
    )
    _write_instruments(cfg, [r])
    clean_instruments(cfg)
    problems = check_specs(cfg)
    assert any("maker" in p for p in problems)
