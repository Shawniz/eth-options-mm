"""Strict fill rule against real trades (CLAUDE.md decision 4; D003, D027).

A taker SELL at p can hit our bid: it fills only if bid > p (at bid == p, the fraction
at_price_fill_fraction, default 0). A taker BUY at p fills our ask only if ask < p.
Eligible size = min(trade size, what is left of our quote). One taker order that sweeps
several levels arrives as several trades at the same timestamp: the quote is used up
across them and is only replenished at a trade after fill_ts + latency.
Fill size = eligible x max_fill_fraction (expected size, D027), or, with
fill_sizing = random, whole contracts drawn so the mean is the same.
Fill price = our quote.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from mm.config import FillConfig

BID_ABOVE = "bid_above_trade"
ASK_BELOW = "ask_below_trade"
AT_PRICE = "at_price"


@dataclass(frozen=True)
class FillDecision:
    our_side: str  # "buy" (taker sold into our bid) or "sell"
    price: float
    eligible: float  # contracts of our quote consumed
    qty: float  # contracts we get
    reason: str


def cross(taker_dir: str, trade_px: float, bid, ask, fcfg: FillConfig):
    """Return (our_side, our_price, fraction, reason) or None if the trade misses us."""
    if taker_dir == "sell":
        if bid is None:
            return None
        if bid > trade_px + 1e-12:
            return "buy", bid, 1.0, BID_ABOVE
        if abs(bid - trade_px) <= 1e-12 and fcfg.at_price_fill_fraction > 0:
            return "buy", bid, fcfg.at_price_fill_fraction, AT_PRICE
        return None
    if ask is None:
        return None
    if ask < trade_px - 1e-12:
        return "sell", ask, 1.0, ASK_BELOW
    if abs(ask - trade_px) <= 1e-12 and fcfg.at_price_fill_fraction > 0:
        return "sell", ask, fcfg.at_price_fill_fraction, AT_PRICE
    return None


def fill_qty(eligible: float, fraction: float, fcfg: FillConfig, sizing: str, rng=None) -> float:
    expected = eligible * fraction * fcfg.max_fill_fraction
    if sizing == "expected":
        return expected
    whole = math.floor(expected)
    return float(whole + (rng.random() < expected - whole))


class QuoteBook:
    """What is left of our quote per (instrument, side), and when it is replenished."""

    def __init__(self, latency_ms: int):
        self.latency_ms = latency_ms
        self._left: dict[tuple[str, str], tuple[float, int]] = {}

    def available(self, instrument: str, side: str, ts_ms: int, size: float) -> float:
        left, until = self._left.get((instrument, side), (size, -1))
        return size if ts_ms > until else min(left, size)

    def consume(self, instrument: str, side: str, ts_ms: int, size: float, used: float) -> None:
        left = self.available(instrument, side, ts_ms, size) - used
        self._left[(instrument, side)] = (max(left, 0.0), ts_ms + self.latency_ms)


def decide(
    instrument: str,
    taker_dir: str,
    trade_px: float,
    trade_qty: float,
    ts_ms: int,
    bid,
    ask,
    size,  # contracts, or (bid_size, ask_size) when limits cut one side
    book: QuoteBook,
    fcfg: FillConfig,
    sizing: str = "expected",
    rng: np.random.Generator | None = None,
) -> FillDecision | None:
    c = cross(taker_dir, trade_px, bid, ask, fcfg)
    if c is None:
        return None
    side, px, frac, reason = c
    if isinstance(size, tuple):
        size = size[0] if side == "buy" else size[1]
    left = book.available(instrument, side, ts_ms, size)
    eligible = min(trade_qty, left)
    if eligible <= 0:
        return None
    book.consume(instrument, side, ts_ms, size, eligible)
    q = fill_qty(eligible, frac, fcfg, sizing, rng)
    if q <= 0:
        return None
    return FillDecision(side, px, eligible, q, reason)
