"""DuckDB views over data/clean.

    con = connect("data/clean")
    con.sql("select date, count(*) from option_trades group by 1")

Trade datasets are hive-partitioned by `date` (UTC day). `ts` is TIMESTAMP WITH TIME ZONE.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

PARTITIONED = ("option_trades", "perp_trades", "future_trades", "dvol")


def connect(clean_dir: str | Path, database: str = ":memory:") -> duckdb.DuckDBPyConnection:
    clean_dir = Path(clean_dir)
    con = duckdb.connect(database)
    con.execute("set TimeZone = 'UTC'")
    for name in PARTITIONED:
        if any((clean_dir / name).glob("date=*/*.parquet")):
            glob = (clean_dir / name / "date=*" / "*.parquet").as_posix()
            con.execute(
                f"create view {name} as select * "
                f"from read_parquet('{glob}', hive_partitioning=true)"
            )
    for f in sorted((clean_dir / "instruments").glob("*.parquet")):
        con.execute(
            f"create view instruments_{f.stem} as select * from read_parquet('{f.as_posix()}')"
        )
    return con
