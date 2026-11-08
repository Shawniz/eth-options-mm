"""Data pipeline CLI.

    uv run python -m mm.data.cli download --config config/base.yaml [--start D --end D] [--sources]
    uv run python -m mm.data.cli clean    --config config/base.yaml [--start D --end D]
    uv run python -m mm.data.cli check    --config config/base.yaml
    uv run python -m mm.data.cli report   --config config/base.yaml

Dates are UTC; --start inclusive, --end exclusive; default to config study_period.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

from mm.config import Config, load_config
from mm.data.clean import (
    CLEAN_SOURCES,
    check_delivery_coverage,
    check_specs,
    check_trades_against_instruments,
    clean_day,
    clean_delivery_prices,
    clean_instruments,
)
from mm.data.client import DeribitClient
from mm.data.db import connect
from mm.data.download import (
    SNAPSHOTS,
    SOURCES,
    download_day,
    download_delivery_prices,
    download_instruments,
    iter_days,
    raw_path,
)

log = logging.getLogger("mm.data")


def _days(cfg: Config, a: argparse.Namespace) -> list[date]:
    start = a.start or cfg.study_period.start
    end = a.end or cfg.study_period.end
    if end <= start:
        raise SystemExit("--end must be after --start")
    return list(iter_days(start, end))


def cmd_download(cfg: Config, a: argparse.Namespace) -> None:
    sources = a.sources or [*SOURCES, *SNAPSHOTS]
    days = _days(cfg, a)
    with (
        DeribitClient(cfg.data.history_base_url, cfg.data.http) as history,
        DeribitClient(cfg.data.main_base_url, cfg.data.http) as main,
    ):
        if "instruments" in sources:
            download_instruments(cfg, history)
        if "delivery_prices" in sources:
            download_delivery_prices(cfg, main)
        for src in (s for s in sources if s not in SNAPSHOTS):
            for i, d in enumerate(days, 1):
                p = download_day(src, d, cfg, history=history, main=main)
                if p is not None and i % 10 == 0:
                    log.info("%s: %d/%d days", src, i, len(days))


def cmd_clean(cfg: Config, a: argparse.Namespace) -> None:
    clean_instruments(cfg)
    clean_delivery_prices(cfg)
    totals: dict[str, dict] = {}
    for src in CLEAN_SOURCES:
        t = totals.setdefault(
            src, {"days": 0, "raw_rows": 0, "kept": 0, "dropped": {}, "suspect": {}}
        )
        for d in _days(cfg, a):
            if not raw_path(cfg.data.raw_dir, src, d).exists():
                log.warning("missing raw %s %s", src, d)
                continue
            r = clean_day(src, d, cfg)
            t["days"] += 1
            t["raw_rows"] += r["raw_rows"]
            t["kept"] += r["kept"]
            for key in ("dropped", "suspect"):
                for k, v in r.get(key, {}).items():
                    t[key][k] = t[key].get(k, 0) + v
    for src, t in totals.items():
        print(
            f"{src:14s} days={t['days']:4d} raw={t['raw_rows']:>11,} kept={t['kept']:>11,} "
            f"dropped={t['dropped']}" + (f" suspect={t['suspect']}" if t["suspect"] else "")
        )


def cmd_check(cfg: Config, a: argparse.Namespace) -> None:
    missing = check_trades_against_instruments(cfg)
    print(f"traded instruments missing from instrument list: {missing or 'none'}")
    no_delivery = check_delivery_coverage(cfg)
    print(f"expired option dates without a delivery price: {no_delivery or 'none'}")
    spec_problems = check_specs(cfg)
    print("config specs vs Deribit records: " + ("OK" if not spec_problems else ""))
    for prob in spec_problems:
        print(f"  MISMATCH {prob}")
    con = connect(cfg.data.clean_dir)
    print(
        con.sql(
            'select count(*) as n, min(ts) as "first", max(ts) as "last", '
            "count(distinct date) as n_days, round(avg(iv), 4) as avg_iv, "
            "count(distinct instrument_name) as n_instruments from option_trades"
        )
    )
    if missing or no_delivery or spec_problems:
        raise SystemExit(1)


def cmd_report(cfg: Config, a: argparse.Namespace) -> None:
    from mm.data.report import write_report

    write_report(cfg, Path("reports/phase1_data.md"), Path("reports/figures/phase1_daily.png"))
    print("wrote reports/phase1_data.md and reports/figures/phase1_daily.png")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="mm.data.cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("command", choices=["download", "clean", "check", "report"])
    p.add_argument("--config", required=True)
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--sources", nargs="+", choices=[*SOURCES, *SNAPSHOTS])
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(a.config)
    cmds = {"download": cmd_download, "clean": cmd_clean, "check": cmd_check, "report": cmd_report}
    cmds[a.command](cfg, a)


if __name__ == "__main__":
    main()
