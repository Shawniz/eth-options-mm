"""The account: ETH balance B, option positions n_i, inverse perpetual (Q, P0).

Invariants (CLAUDE.md): B changes only through premiums, fees, funding, settlement and
perpetual P&L, each posted to the ledger; positions change only through fills, hedges
and expiry. `check()` runs after every event; `audit()` recomputes everything from the
ledger and fill log at the end of a run.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

FLOW_KINDS = frozenset(
    {"premium", "fee", "settlement", "delivery_fee", "funding", "perp_pnl", "perp_fee"}
)
POSITION_REASONS = frozenset({"fill", "expiry", "hedge"})


class InvariantError(AssertionError):
    pass


@dataclass
class Account:
    initial_eth: float
    perp_usd: float = 0.0  # Q, USD notional, long > 0 (Phase 6)
    perp_entry: float = 0.0  # P0
    _eth: float = field(init=False)
    positions: dict[str, float] = field(default_factory=dict)
    ledger: list[tuple[int, str, str, float]] = field(default_factory=list)
    position_log: list[tuple[int, str, str, float]] = field(default_factory=list)
    _flow_sum: float = field(init=False, default=0.0)

    def __post_init__(self):
        self._eth = self.initial_eth

    @property
    def eth(self) -> float:
        return self._eth

    def post(self, ts_ms: int, kind: str, amount_eth: float, ref: str = "") -> None:
        if kind not in FLOW_KINDS:
            raise InvariantError(f"ETH balance changed by an unknown flow {kind!r}")
        if not math.isfinite(amount_eth):
            raise InvariantError(f"non-finite {kind} amount {amount_eth}")
        self._eth += amount_eth
        self._flow_sum += amount_eth
        self.ledger.append((ts_ms, kind, ref, amount_eth))

    def move(self, ts_ms: int, reason: str, instrument: str, qty: float) -> None:
        if reason not in POSITION_REASONS:
            raise InvariantError(f"position changed for an unknown reason {reason!r}")
        n = self.positions.get(instrument, 0.0) + qty
        if abs(n) < 1e-12:
            self.positions.pop(instrument, None)
        else:
            self.positions[instrument] = n
        self.position_log.append((ts_ms, reason, instrument, qty))

    def option_fill(self, ts_ms, instrument, our_side, qty, price_eth, fee_eth) -> None:
        signed = qty if our_side == "buy" else -qty
        self.post(ts_ms, "premium", -signed * price_eth, instrument)
        self.post(ts_ms, "fee", -fee_eth, instrument)
        self.move(ts_ms, "fill", instrument, signed)

    def check(self) -> None:
        if abs(self._eth - (self.initial_eth + self._flow_sum)) > 1e-9 * max(1.0, abs(self._eth)):
            raise InvariantError("ETH balance differs from initial + sum of ledger flows")

    def audit(self) -> None:
        """Recompute balance and positions from scratch (end of run)."""
        eth = self.initial_eth + math.fsum(a for *_, a in self.ledger)
        if abs(eth - self._eth) > 1e-8 * max(1.0, abs(eth)):
            raise InvariantError(f"ledger gives {eth}, balance is {self._eth}")
        pos: dict[str, float] = defaultdict(float)
        for _, _, inst, q in self.position_log:
            pos[inst] += q
        pos = {k: v for k, v in pos.items() if abs(v) > 1e-12}
        if pos.keys() != self.positions.keys() or any(
            abs(pos[k] - self.positions[k]) > 1e-9 for k in pos
        ):
            raise InvariantError("positions differ from the fill/expiry log")
