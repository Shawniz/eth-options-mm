from datetime import date, timedelta

from conftest import make_trade

from mm.data.clean import clean_day
from mm.data.db import connect
from mm.data.download import day_bounds_ms, raw_path, trades_to_table, write_raw


def test_duckdb_views_over_clean_partitions(cfg):
    for k in range(2):
        day = date(2025, 1, 1) + timedelta(days=k)
        s, _ = day_bounds_ms(day)
        rows = [make_trade(s + i) for i in range(3)]
        write_raw(trades_to_table(rows), raw_path(cfg.data.raw_dir, "option_trades", day))
        clean_day("option_trades", day, cfg)

    con = connect(cfg.data.clean_dir)
    n, days = con.execute("select count(*), count(distinct date) from option_trades").fetchone()
    assert (n, days) == (6, 2)
    tz = con.execute("select typeof(ts) from option_trades limit 1").fetchone()[0]
    assert tz == "TIMESTAMP WITH TIME ZONE"
