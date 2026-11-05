"""Small, dependency-free statistics used to tell real differences from noise.

Everything here is deterministic. Methods:
  * Wilson score interval for proportions (behaves well for small n and rates near 0/1).
  * Fisher's exact test (two-sided) for comparing two proportions; exact at any n.
  * Normal-approximation interval and Welch z-test for means (revenue per contact).
  * Fisher z-transform interval and p-value for Pearson correlations.
  * Benjamini-Hochberg adjustment when many groups are tested at once, so testing
    ten campaigns doesn't manufacture a "winner" by chance.
"""
from __future__ import annotations

import math

Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    if not n:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _log_comb(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher_exact(k1: int, n1: int, k2: int, n2: int) -> float | None:
    """Two-sided p-value for H0: the two groups share one success rate."""
    if n1 <= 0 or n2 <= 0:
        return None
    k = k1 + k2
    n = n1 + n2
    lo, hi = max(0, k - n2), min(k, n1)
    denom = _log_comb(n, k)

    def logp(x: int) -> float:
        return _log_comb(n1, x) + _log_comb(n2, k - x) - denom

    observed = logp(k1)
    total = 0.0
    for x in range(lo, hi + 1):
        lp = logp(x)
        if lp <= observed + 1e-7:
            total += math.exp(lp)
    return min(1.0, total)


def normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def mean_ci(values: list[float], z: float = Z95) -> tuple[float, float] | None:
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    half = z * math.sqrt(var / n)
    return mean - half, mean + half


def welch_z(a: list[float], b: list[float]) -> float | None:
    """Two-sided p-value for a difference in means (large-sample normal approximation)."""
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return None if ma == mb else 0.0
    return 2 * (1 - normal_cdf(abs(ma - mb) / se))


def correlation_ci_p(r: float | None, n: int, z: float = Z95) -> tuple[tuple[float, float] | None, float | None]:
    if r is None or n < 4 or abs(r) >= 1:
        return None, None
    fz = math.atanh(r)
    se = 1 / math.sqrt(n - 3)
    ci = (math.tanh(fz - z * se), math.tanh(fz + z * se))
    p = 2 * (1 - normal_cdf(abs(fz) / se))
    return ci, p


def bh_adjust(pvalues: list[float | None]) -> list[float | None]:
    """Benjamini-Hochberg adjusted p-values (q-values); None entries are passed through."""
    idx = [i for i, p in enumerate(pvalues) if p is not None]
    m = len(idx)
    out: list[float | None] = [None] * len(pvalues)
    if not m:
        return out
    order = sorted(idx, key=lambda i: pvalues[i])
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        out[i] = min(1.0, running)
    return out
