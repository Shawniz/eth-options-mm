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
    import logging
    from pathlib import Path

    import pandas as pd

    from mm.sim.engine import run
    from mm.sim.load import load_inputs
    from mm.sim.report import save, write_report, write_summary

    args = parse_args(argv)
    cfg = load_config(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    label = f"{args.start:%Y-%m-%d}_{args.end:%Y-%m-%d}"
    print(f"Run {label} (UTC), config {args.config}")
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    inp = load_inputs(cfg, start, end)
    res = run(inp, cfg)
    run_dir = Path(cfg.sim.run_dir) / label
    save(res, run_dir)
    summary = write_report(cfg, inp, res, label, Path(f"reports/sim_{label}.md"))
    write_summary(summary, run_dir)
    from mm.sim.figures import delta_figure, vega_figure

    figs = Path("reports/figures")
    delta_figure(cfg, inp, res, figs / f"sim_{label}_delta.png")
    vega_figure(cfg, res, figs / f"sim_{label}_vega.png")
    print(
        f"fills {summary['fills']:,}, share {summary['share_of_contracts'] * 100:.2f}%, "
        f"hedges {summary['hedging']['hedges']:,}, final ETH {summary['final_eth']:.4f} "
        f"-> {run_dir}"
    )
    return cfg


if __name__ == "__main__":
    main()
