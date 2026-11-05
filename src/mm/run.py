"""Single entry point: uv run python -m mm.run --config config/base.yaml --start ... --end ..."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime

from mm.config import Config, load_config


def _parse_date(s: str) -> datetime:
    return datetime.combine(date.fromisoformat(s), datetime.min.time(), tzinfo=UTC)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="mm.run", description=__doc__)
    p.add_argument("--config", required=True, help="Path to a YAML config")
    p.add_argument("--start", required=True, type=_parse_date, help="UTC start date YYYY-MM-DD")
    p.add_argument("--end", required=True, type=_parse_date, help="UTC end date YYYY-MM-DD")
    args = p.parse_args(argv)
    if args.end <= args.start:
        p.error("--end must be after --start")
    return args


def main(argv: list[str] | None = None) -> Config:
    args = parse_args(argv)
    cfg = load_config(args.config)
    print(f"Run {args.start:%Y-%m-%d} -> {args.end:%Y-%m-%d} (UTC)")
    print(cfg.model_dump_json(indent=2))
    # Phases 1+ plug the pipeline in here.
    return cfg


if __name__ == "__main__":
    main()
