"""Parse Deribit ETH instrument names.

Formats:
    ETH-28FEB25-4500-C   option (strike in USD; 'd' marks a decimal point, e.g. 2d5 = 2.5)
    ETH-27JUN25          dated future
    ETH-PERPETUAL        perpetual
Deribit expiries are at 08:00 UTC on the expiry date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

EXPIRY_HOUR_UTC = 8  # Deribit settlement time; fixed by the exchange, not a model parameter

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
        start=1,
    )
}
_DATE = r"(?P<day>\d{1,2})(?P<mon>[A-Z]{3})(?P<yy>\d{2})"
_OPTION_RE = re.compile(rf"^ETH-{_DATE}-(?P<strike>\d+(?:d\d+)?)-(?P<cp>[CP])$")
_FUTURE_RE = re.compile(rf"^ETH-{_DATE}$")


@dataclass(frozen=True)
class Instrument:
    name: str
    kind: Literal["option", "future", "perpetual"]
    expiry: datetime | None
    strike_usd: float | None = None
    option_type: Literal["C", "P"] | None = None

    @property
    def expiry_ms(self) -> int | None:
        return None if self.expiry is None else int(self.expiry.timestamp() * 1000)


def _expiry(m: re.Match, name: str) -> datetime:
    mon = _MONTHS.get(m["mon"])
    if mon is None:
        raise ValueError(f"bad month in {name!r}")
    try:
        return datetime(2000 + int(m["yy"]), mon, int(m["day"]), EXPIRY_HOUR_UTC, tzinfo=UTC)
    except ValueError as e:
        raise ValueError(f"bad date in {name!r}: {e}") from None


def parse_instrument(name: str) -> Instrument:
    if name == "ETH-PERPETUAL":
        return Instrument(name, "perpetual", None)
    if m := _OPTION_RE.match(name):
        strike = float(m["strike"].replace("d", "."))
        return Instrument(name, "option", _expiry(m, name), strike, m["cp"])  # type: ignore[arg-type]
    if m := _FUTURE_RE.match(name):
        return Instrument(name, "future", _expiry(m, name))
    raise ValueError(f"unrecognised ETH instrument name {name!r}")
