"""Raw SVI smile in total variance:  w(k) = a + b (rho (k - m) + sqrt((k - m)^2 + s^2)).

k = ln(K / F) is log-moneyness on the expiry's forward; w = sigma^2 T is total variance.
Constraints: b >= 0, |rho| < 1, s > 0, a + b s sqrt(1 - rho^2) >= 0 (so w >= 0).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class SVI:
    a: float
    b: float
    rho: float
    m: float
    s: float

    def scaled(self, factor: float) -> SVI:
        """Multiply total variance by `factor` (a and b scale, shape unchanged). Used to
        keep vols fixed when time passes: factor = T_new / T_old (sticky vol)."""
        return SVI(self.a * factor, self.b * factor, self.rho, self.m, self.s)

    @property
    def min_variance(self) -> float:
        return self.a + self.b * self.s * np.sqrt(1.0 - self.rho**2)

    def as_tuple(self) -> tuple[float, float, float, float, float]:
        return (self.a, self.b, self.rho, self.m, self.s)


def svi_w(p: SVI, k, d: int = 0):
    """Total variance (d=0) or its first/second derivative in k (d=1, 2)."""
    x = np.asarray(k, dtype=float) - p.m
    r = np.sqrt(x * x + p.s * p.s)
    if d == 0:
        out = p.a + p.b * (p.rho * x + r)
    elif d == 1:
        out = p.b * (p.rho + x / r)
    elif d == 2:
        out = p.b * p.s * p.s / r**3
    else:
        raise ValueError("d must be 0, 1 or 2")
    return float(out) if np.ndim(out) == 0 else out


def svi_vol(p: SVI, k, t: float):
    """Implied vol at log-moneyness k for time to expiry t (years)."""
    w = np.asarray(svi_w(p, k))
    with np.errstate(invalid="ignore"):
        out = np.sqrt(np.maximum(w, 0.0) / t)
    return float(out) if np.ndim(out) == 0 else out


def butterfly_g(p: SVI, k) -> np.ndarray:
    """Gatheral's density function g(k); g < 0 means negative risk-neutral density
    (a butterfly spread with negative cost)."""
    k = np.asarray(k, dtype=float)
    w, w1, w2 = svi_w(p, k), svi_w(p, k, 1), svi_w(p, k, 2)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (1 - k * w1 / (2 * w)) ** 2 - (w1**2 / 4) * (1 / w + 0.25) + w2 / 2
