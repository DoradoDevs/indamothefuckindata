"""Code-level verification of Claude's findings against the computed metrics.

Runs before the Claude reviewer pass. A finding fails if:
  * it cites a metric ID that doesn't exist,
  * a quoted number doesn't match its metric (beyond rounding tolerance),
  * a number appears in the claim text that isn't backed by a quoted metric,
  * it rests on a small sample and doesn't say so in caveats.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pipeline.analyze.schemas import Finding
from pipeline.metrics.compute import Metric

_NUM = re.compile(r"(?<![\w.])[-+]?\$?\d[\d,]*(?:\.\d+)?%?")
_SMALL_SAMPLE_WORDS = ("sample", "small n", "n=", "few ", "limited data", "low volume", "only ")


@dataclass
class CheckResult:
    finding_id: str
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _close(a: float, b: float, tol: float) -> bool:
    if a == b:
        return True
    return abs(a - b) <= max(abs(b) * tol, 0.005)


def _candidates(metric_value: float, unit: str) -> list[float]:
    """Acceptable renderings of a metric: as-is, and ratios as percentages."""
    c = [metric_value]
    if unit == "ratio":
        c.append(metric_value * 100)
    return c


def _parse_claim_numbers(text: str) -> list[float]:
    out = []
    for tok in _NUM.findall(text):
        raw = tok.replace("$", "").replace(",", "").rstrip("%")
        try:
            out.append(float(raw))
        except ValueError:
            pass
    return out


def check_finding(f: Finding, metrics: dict[str, Metric], min_sample: int, tol: float) -> CheckResult:
    r = CheckResult(f.finding_id, True)

    for mid in set(f.evidence) | {q.metric_id for q in f.quoted_numbers}:
        if mid not in metrics:
            r.errors.append(f"unknown metric id: {mid}")

    backed: list[float] = []
    for q in f.quoted_numbers:
        m = metrics.get(q.metric_id)
        if m is None or m.value is None:
            continue
        if not any(_close(q.value, c, tol) for c in _candidates(m.value, m.unit)):
            r.errors.append(f"{q.metric_id}: quoted {q.value}, metric is {m.value}")
        backed.extend(_candidates(m.value, m.unit) + [q.value])

    # Numbers in the claim text must trace to a quoted metric. Small integers (1-12)
    # are allowed unbacked since they're usually counts of things or month lags.
    for n in _parse_claim_numbers(f.claim):
        if abs(n) <= 12 and float(n).is_integer():
            continue
        if 1900 <= n <= 2100:  # years
            continue
        if not any(_close(n, b, tol) for b in backed):
            r.errors.append(f"number {n} in claim is not backed by a quoted metric")

    thin = [mid for mid in f.evidence if mid in metrics and metrics[mid].n < min_sample]
    if thin:
        caveat_text = " ".join(f.caveats).lower()
        if not any(w in caveat_text for w in _SMALL_SAMPLE_WORDS):
            r.errors.append(f"relies on small samples (n<{min_sample}) without a caveat: {', '.join(thin)}")
        else:
            r.warnings.append(f"small samples: {', '.join(thin)}")

    r.passed = not r.errors
    return r


def check_all(findings: list[Finding], metrics: list[Metric], min_sample: int, tol: float) -> list[CheckResult]:
    by_id = {m.id: m for m in metrics}
    return [check_finding(f, by_id, min_sample, tol) for f in findings]
