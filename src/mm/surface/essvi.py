"""eSSVI slices (Hendriks & Martini 2019): SSVI with a per-expiry skew.

    w(k) = theta/2 * (1 + rho*phi*k + sqrt((phi*k + rho)^2 + 1 - rho^2)),  phi = psi/theta

theta = ATM total variance, rho = skew, psi = theta*phi (ATM slope scale). Each slice is a
raw SVI smile with tied parameters (Gatheral & Jacquier 2014, Lemma 3.3):
    a = theta (1 - rho^2) / 2,  b = psi / 2,  m = -rho / phi,  s = sqrt(1 - rho^2) / phi

No butterfly arbitrage at any k (Gatheral & Jacquier 2014, Thm 4.2) if
    psi (1 + |rho|) < 4   and   psi^2 (1 + |rho|) <= 4 theta            -> psi <= psi_max
Calendar (shorter slice 1, longer slice 2). NECESSARY conditions, used as hard bounds:
    theta2 >= theta1, and both asymptotic wing slopes ordered:
    psi2 (1 - rho2) >= psi1 (1 - rho1) and psi2 (1 + rho2) >= psi1 (1 + rho1) -> psi >= psi_min
These are NOT sufficient (a property test found a counterexample), so the fitter also
enforces w2(k) >= w1(k) on a dense grid (decision D023) and every refit is re-checked.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from mm.surface.svi import SVI

_STRICT = 1.0 - 1e-9  # psi (1 + |rho|) < 4 is strict
PSI_FLOOR = 1e-10


@dataclass(frozen=True)
class ESSVI:
    theta: float
    rho: float
    psi: float

    def to_svi(self) -> SVI:
        phi = self.psi / self.theta
        return SVI(
            a=self.theta * (1 - self.rho**2) / 2,
            b=self.psi / 2,
            rho=self.rho,
            m=-self.rho / phi,
            s=np.sqrt(1 - self.rho**2) / phi,
        )

    def scaled(self, factor: float) -> ESSVI:
        """Total variance times `factor`, same shape in k (theta and psi scale)."""
        return ESSVI(self.theta * factor, self.rho, self.psi * factor)


def psi_max(theta, rho):
    """Largest psi with no butterfly arbitrage."""
    r = 1.0 + np.abs(rho)
    return np.minimum(4.0 / r * _STRICT, 2.0 * np.sqrt(np.maximum(theta, 0.0) / r))


def psi_min(rho, shorter: ESSVI | None):
    """Smallest psi whose wing slopes are both >= the shorter slice's (necessary for
    no calendar arbitrage, not sufficient)."""
    if shorter is None:
        return PSI_FLOOR
    lo = max(
        shorter.psi * (1 - shorter.rho) / (1 - rho),
        shorter.psi * (1 + shorter.rho) / (1 + rho),
    )
    return max(lo, PSI_FLOOR)


def project(e: ESSVI, shorter: ESSVI | None) -> ESSVI:
    """Nearest slice (same rho) satisfying the bound conditions. If they cannot both hold,
    the calendar bound wins (psi = psi_min) and the numeric check flags any violation."""
    theta = max(e.theta, shorter.theta) if shorter is not None else e.theta
    lo, hi = psi_min(e.rho, shorter), float(psi_max(theta, e.rho))
    psi = min(max(e.psi, lo), hi) if lo <= hi else lo
    return ESSVI(theta, e.rho, psi)
