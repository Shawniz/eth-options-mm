from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from mm.config import Config, load_config

BASE = Path(__file__).resolve().parents[1] / "config" / "base.yaml"

_ids = itertools.count(1000)


def make_trade(ts: int, instrument: str = "ETH-28FEB25-4500-C", **extra) -> dict:
    """A trade dict shaped like the Deribit history API response."""
    n = next(_ids)
    t = {
        "trade_seq": n,
        "trade_id": f"ETH-{n}",
        "timestamp": ts,
        "tick_direction": 1,
        "price": 0.031,
        "mark_price": 0.0312,
        "iv": 74.37,
        "instrument_name": instrument,
        "index_price": 3330.76,
        "direction": "buy",
        "contracts": 12.0,
        "amount": 12.0,
    }
    t.update(extra)
    return t


@pytest.fixture
def cfg(tmp_path) -> Config:
    base = load_config(BASE)
    data = base.data.model_copy(
        update={"raw_dir": tmp_path / "raw", "clean_dir": tmp_path / "clean"}
    )
    return base.model_copy(update={"data": data})


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
