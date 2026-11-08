"""One-off: snapshot live Deribit ETH option tickers into a committed test fixture.

Approved by the owner (2026-11-08) as the only way to check Greeks, which Deribit does not
publish historically. This is NOT live recording: it runs once, writes a JSON file under
tests/fixtures/, and nothing in the backtest reads it.

    uv run python scripts/make_deribit_fixture.py --config config/base.yaml
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from mm.config import load_config
from mm.data.client import DeribitClient
from mm.data.instruments import parse_instrument
from mm.pricing.time import MS_PER_YEAR

PER_EXPIRY = 14  # strikes sampled per expiry (calls and puts alternate)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    cfg = load_config(a.config)
    today = datetime.now(UTC).date()
    out = Path(a.out or f"tests/fixtures/deribit_tickers_{today}.json")
    if out.exists():
        raise SystemExit(f"{out} exists; fixtures are written once")

    with DeribitClient(cfg.data.main_base_url, cfg.data.http) as c:
        book = c.get("public/get_book_summary_by_currency", {"currency": "ETH", "kind": "option"})
        by_exp: dict[int, list[dict]] = {}
        for b in book:
            try:
                ins = parse_instrument(b["instrument_name"])
            except ValueError:
                continue
            by_exp.setdefault(ins.expiry_ms, []).append({**b, "_ins": ins})

        rows = []
        for exp_ms in sorted(by_exp):
            opts = sorted(by_exp[exp_ms], key=lambda b: b["_ins"].strike_usd)
            # spread picks evenly across the strike range, alternating call/put
            picks = np.unique(np.linspace(0, len(opts) - 1, PER_EXPIRY * 2).round().astype(int))
            chosen = [opts[i] for i in picks]
            for j, b in enumerate(chosen):
                want = "C" if j % 2 == 0 else "P"
                if b["_ins"].option_type != want:
                    continue
                t = c.get("public/ticker", {"instrument_name": b["instrument_name"]})
                if not t.get("mark_iv") or t["mark_price"] <= 0:
                    continue
                ins = b["_ins"]
                rows.append(
                    {
                        "instrument_name": t["instrument_name"],
                        "timestamp": t["timestamp"],
                        "expiry_ms": ins.expiry_ms,
                        "t_years": (ins.expiry_ms - t["timestamp"]) / MS_PER_YEAR,
                        "strike": ins.strike_usd,
                        "option_type": "call" if ins.option_type == "C" else "put",
                        "underlying_price": t["underlying_price"],
                        "underlying_index": t["underlying_index"],
                        "index_price": t["index_price"],
                        "interest_rate": t["interest_rate"],
                        "mark_iv": t["mark_iv"],
                        "mark_price": t["mark_price"],
                        "greeks": t["greeks"],
                    }
                )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1))
    print(f"wrote {len(rows)} tickers across {len(by_exp)} expiries to {out}")


if __name__ == "__main__":
    main()
