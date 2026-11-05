from datetime import date

import pyarrow.parquet as pq
import pytest
from conftest import make_trade

from mm.data.download import (
    day_bounds_ms,
    download_day,
    fetch_trades,
    raw_path,
    trades_to_table,
    write_raw,
)

DAY = date(2025, 1, 1)
S, E = day_bounds_ms(DAY)


class FakeTradesClient:
    """Serves trades like the history API: inclusive bounds, ascending, timestamp paging."""

    def __init__(self, trades):
        self.trades = sorted(trades, key=lambda t: t["timestamp"])
        self.calls = []

    def get(self, method, params):
        self.calls.append((method, dict(params)))
        lo, hi, n = params["start_timestamp"], params["end_timestamp"], params["count"]
        hits = [t for t in self.trades if lo <= t["timestamp"] <= hi]
        return {"trades": hits[:n], "has_more": len(hits) > n}


def test_day_bounds_are_inclusive_ms():
    assert S == 1735689600000
    assert E == S + 86_400_000 - 1


def test_fetch_pages_and_dedupes_boundary(cfg):
    # 5 trades share the page-boundary timestamp; page size 4 forces overlap.
    trades = [make_trade(S + i) for i in range(3)] + [make_trade(S + 10) for _ in range(5)]
    trades += [make_trade(S + 20 + i) for i in range(4)]
    client = FakeTradesClient(trades)
    got = fetch_trades(client, "public/x", {}, S, E, page_size=6)
    assert len(got) == len(trades)
    assert len({t["trade_id"] for t in got}) == len(trades)
    assert [t["timestamp"] for t in got] == sorted(t["timestamp"] for t in got)
    assert len(client.calls) > 1


def test_fetch_raises_when_page_cannot_advance(cfg):
    trades = [make_trade(S + 5) for _ in range(10)]
    with pytest.raises(RuntimeError, match="cannot advance"):
        fetch_trades(FakeTradesClient(trades), "public/x", {}, S, E, page_size=4)


def test_fetch_drops_trades_outside_window():
    trades = [make_trade(S - 1), make_trade(S), make_trade(E), make_trade(E + 1)]

    class Leaky(FakeTradesClient):
        def get(self, method, params):
            return {"trades": self.trades, "has_more": False}

    got = fetch_trades(Leaky(trades), "public/x", {}, S, E, page_size=100)
    assert [t["timestamp"] for t in got] == [S, E]


def test_table_keeps_optional_and_unknown_fields():
    rows = [
        make_trade(S),
        make_trade(S + 1, block_trade_id="BLOCK-1", block_trade_leg_count=1),
        make_trade(S + 2, combo_id="ETH-CS-1", combo_trade_id="x", liquidation="M"),
        make_trade(S + 3, brand_new_field=42),
    ]
    tbl = trades_to_table(rows)
    d = tbl.to_pydict()
    assert d["block_trade_id"] == [None, "BLOCK-1", None, None]
    assert d["combo_id"][2] == "ETH-CS-1"
    assert d["liquidation"][2] == "M"
    assert '"brand_new_field": 42' in d["extra_json"][3]
    assert d["extra_json"][0] is None


def test_empty_day_still_has_schema():
    tbl = trades_to_table([])
    assert tbl.num_rows == 0
    assert "timestamp" in tbl.schema.names


def test_write_raw_never_overwrites(tmp_path):
    p = tmp_path / "a" / "part.parquet"
    write_raw(trades_to_table([make_trade(S)]), p)
    with pytest.raises(FileExistsError):
        write_raw(trades_to_table([make_trade(S)]), p)
    assert not list(p.parent.glob("*.tmp"))


def test_download_day_writes_then_skips(cfg):
    client = FakeTradesClient([make_trade(S + 1), make_trade(S + 2)])
    p = download_day("option_trades", DAY, cfg, history=client, main=None)
    assert p == raw_path(cfg.data.raw_dir, "option_trades", DAY)
    assert pq.read_table(p).num_rows == 2
    n_calls = len(client.calls)
    assert download_day("option_trades", DAY, cfg, history=client, main=None) is None
    assert len(client.calls) == n_calls  # skipped, no network


def test_download_option_request_params(cfg):
    client = FakeTradesClient([])
    download_day("option_trades", DAY, cfg, history=client, main=None)
    method, params = client.calls[0]
    assert method == "public/get_last_trades_by_currency_and_time"
    assert params["currency"] == "ETH" and params["kind"] == "option"
    assert params["sorting"] == "asc"


def test_download_refuses_incomplete_day(cfg):
    with pytest.raises(ValueError, match="not complete"):
        download_day("option_trades", date(2999, 1, 1), cfg, history=None, main=None)


def test_download_dvol_follows_continuation(cfg):
    class FakeMain:
        def __init__(self):
            self.calls = []

        def get(self, method, params):
            self.calls.append(dict(params))
            if len(self.calls) == 1:
                return {"data": [[S + 7_200_000, 70, 71, 69, 70.5]], "continuation": S + 3_600_000}
            return {
                "data": [[S, 70, 70, 70, 70], [S + 3_600_000, 1, 2, 0.5, 1.5]],
                "continuation": None,
            }

    main = FakeMain()
    p = download_day("dvol", DAY, cfg, history=None, main=main)
    d = pq.read_table(p).to_pydict()
    assert d["timestamp"] == [S, S + 3_600_000, S + 7_200_000]
    assert main.calls[1]["end_timestamp"] == S + 3_600_000
    assert main.calls[0]["resolution"] == cfg.data.dvol_resolution_s
