"""Data pipeline CLI.

    uv run python -m mm.data.cli download --config config/base.yaml [--start D --end D] [--sources]
    uv run python -m mm.data.cli clean    --config config/base.yaml [--start D --end D]
    uv run python -m mm.data.cli check    --config config/base.yaml

Dates are UTC; --start inclusive, --end exclusive; default to config study_period.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date

from mm.config import Config, load_config
from mm.data.clean import (
    CLEAN_SOURCES,
    check_trades_against_instruments,
    clean_day,
    clean_instruments,
)
from mm.data.client import DeribitClient
from mm.data.db import connect
from mm.data.download import SOURCES, download_day, download_instruments, iter_days, raw_path

log = logging.getLogger("mm.data")


def _days(cfg: Config, a: argparse.Namespace) -> list[date]:
    start = a.start or cfg.study_period.start
    end = a.end or cfg.study_period.end
    if end <= start:
        raise SystemExit("--end must be after --start")
    return list(iter_days(start, end))


def cmd_download(cfg: Config, a: argparse.Namespace) -> None:
    sources = a.sources or [*SOURCES, "instruments"]
    days = _days(cfg, a)
    with (
        DeribitClient(cfg.data.history_base_url, cfg.data.http) as history,
        DeribitClient(cfg.data.main_base_url, cfg.data.http) as main,
    ):
        if "instruments" in sources:
            download_instruments(cfg, history)
        for src in (s for s in sources if s != "instruments"):
            for i, d in enumerate(days, 1):
                p = download_day(src, d, cfg, history=history, main=main)
                if p is not None and i % 10 == 0:
                    log.info("%s: %d/%d days", src, i, len(days))


def cmd_clean(cfg: Config, a: argparse.Namespace) -> None:
    clean_instruments(cfg)
    totals: dict[str, dict] = {}
    for src in CLEAN_SOURCES:
        t = totals.setdefault(src, {"days": 0, "raw_rows": 0, "kept": 0, "dropped": {}})
        for d in _days(cfg, a):
            if not raw_path(cfg.data.raw_dir, src, d).exists():
                log.warning("missing raw %s %s", src, d)
                continue
            r = clean_day(src, d, cfg)
            t["days"] += 1
            t["raw_rows"] += r["raw_rows"]
            t["kept"] += r["kept"]
            for k, v in r["dropped"].items():
                t["dropped"][k] = t["dropped"].get(k, 0) + v
    for src, t in totals.items():
        print(
            f"{src:14s} days={t['days']:4d} raw={t['raw_rows']:>11,} kept={t['kept']:>11,} "
            f"dropped={t['dropped']}"
        )


def cmd_check(cfg: Config, a: argparse.Namespace) -> None:
    missing = check_trades_against_instruments(cfg)
    print(f"traded instruments missing from instrument list: {missing or 'none'}")
    con = connect(cfg.data.clean_dir)
    print(
        con.sql(
            'select count(*) as n, min(ts) as "first", max(ts) as "last", '
            "count(distinct date) as n_days, round(avg(iv), 4) as avg_iv, "
            "count(distinct instrument_name) as n_instruments from option_trades"
        )
    )
    if missing:
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="mm.data.cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("command", choices=["download", "clean", "check"])
    p.add_argument("--config", required=True)
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--sources", nargs="+", choices=[*SOURCES, "instruments"])
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(a.config)
    {"download": cmd_download, "clean": cmd_clean, "check": cmd_check}[a.command](cfg, a)


if __name__ == "__main__":
    main()
