"""Code-level verification of Claude's findings against the computed metrics.

Runs before the Claude reviewer pass. A finding fails if:
  * it cites a metric ID that doesn't exist,
  * a quoted number doesn't match its metric (beyond rounding tolerance),
  * a number appears in the claim text that isn't backed by a quoted metric,
  * it rests on a small sample and doesn't say so in caveats,
  * it compares groups (e.g. two campaigns' win rates) without either citing a
    significance test that backs the difference or saying it could be chance,
  * it calls something "significant" without a significant test behind it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pipeline.analyze.schemas import Finding
from pipeline.metrics.compute import Metric

_NUM = re.compile(r"(?<![\w.])[-+]?\$?\d[\d,]*(?:\.\d+)?%?")
_SMALL_SAMPLE_WORDS = ("sample", "small n", "n=", "few ", "limited data", "low volume", "only ", "just ",
                       "too small", "too thin", "small group", "small number")
_CHANCE_WORDS = ("chance", "not significant", "not statistically", "noise", "uncertain", "could be random",
                 "not a reliable difference", "may not be real")
_SIGNIF_CLAIM = re.compile(r"\b(statistically|significant(ly)?)\b", re.I)
_GROUP_METRIC = re.compile(
    r"^(campaign|source|segment|industry|pipeline|engagement|form|service\.campaign|service\.source)\.([^.]+)\.(\w+)$")
# Measures that have a test.<dim>.<key>.<measure> metric.
TESTED = {
    "campaign": {"contact_to_deal_rate", "win_rate", "revenue_per_contact"},
    "source": {"contact_to_won_rate", "revenue_per_contact", "converted_rate", "email_click_rate", "email_open_rate",
               "open_rate_emailed", "click_rate_emailed"},
    "engagement": {"email_open_rate", "email_click_rate", "form_rate"},
    "segment": {"converted_rate", "open_rate_emailed", "click_rate_emailed"},
    "form": {"converted_rate"},
    "industry": {"win_rate"},
    "service_campaign": {"contact_to_service_win_rate", "service_value_per_contact"},
    "service_source": {"contact_to_service_win_rate", "service_value_per_contact"},
}


@dataclass
class CheckResult:
    finding_id: str
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _close(a: float, b: float, tol: float, decimals: int | None = None) -> bool:
    """Equal within relative tolerance, or equal at the precision the number was written."""
    if a == b:
        return True
    if abs(a - b) <= max(abs(b) * tol, 0.005):
        return True
    return decimals is not None and abs(a - b) <= 0.5 * 10 ** -decimals + 1e-9


def _candidates(value: float, unit: str) -> list[float]:
    """Acceptable renderings: as-is, size of a signed difference ("0.12 below"),
    and ratios as percentages."""
    c = [value, abs(value)]
    if unit == "ratio":
        c += [value * 100, abs(value) * 100]
    return c


def _metric_values(mt: Metric) -> list[float]:
    """Everything a metric legitimately supplies: value, 95% range ends, adjusted p-value,
    and its sample size (findings often say "n=12" next to a rate)."""
    out: list[float] = []
    for v in (mt.value, mt.ci_low, mt.ci_high):
        if v is not None:
            out += _candidates(v, mt.unit)
    if mt.p_value is not None:
        out.append(mt.p_value)
    if mt.n is not None:
        out.append(float(mt.n))
    return out


# Constants that describe the method rather than the data ("95% range", "5% level").
_METHOD_CONSTANTS = {95.0, 0.05}


def _parse_claim_numbers(text: str) -> list[tuple[float, int]]:
    """(number, decimals as written) for each number in the text."""
    out = []
    for tok in _NUM.findall(text):
        raw = tok.replace("$", "").replace(",", "").rstrip("%")
        try:
            out.append((float(raw), len(raw.split(".")[1]) if "." in raw else 0))
        except ValueError:
            pass
    return out


def check_finding(f: Finding, metrics: dict[str, Metric], min_sample: int, tol: float) -> CheckResult:
    r = CheckResult(f.finding_id, True)

    for mid in set(f.evidence) | {q.metric_id for q in f.quoted_numbers}:
        if mid not in metrics:
            r.errors.append(f"unknown metric id: {mid}")

    # Anything a cited metric supplies may appear in the text.
    backed: list[float] = []
    for mid in set(f.evidence) | {q.metric_id for q in f.quoted_numbers}:
        if mid in metrics:
            backed += _metric_values(metrics[mid])
    for q in f.quoted_numbers:
        m = metrics.get(q.metric_id)
        if m is None or m.value is None:
            continue
        if not any(_close(q.value, c, tol) for c in _metric_values(m)):
            r.errors.append(f"{q.metric_id}: quoted {q.value}, metric is {m.value}")
        backed.append(q.value)

    # Numbers in the claim text must trace to a cited metric. Small integers (1-12)
    # are allowed unbacked since they're usually counts of things or month lags.
    for n, decimals in _parse_claim_numbers(f.claim):
        if abs(n) <= 12 and float(n).is_integer():
            continue
        if 1900 <= n <= 2100 or n in _METHOD_CONSTANTS:  # years, method constants
            continue
        if not any(_close(n, b, tol, decimals) for b in backed):
            r.errors.append(f"number {n} in claim is not backed by a quoted metric")

    # Small-sample caveats apply to estimates (rates, means, correlations: anything with a
    # range or a test), not to exact totals and counts, which describe everything there is.
    def is_estimate(mid: str) -> bool:
        mt = metrics[mid]
        if mid.startswith("quality.") or ".share_of_" in mid:
            return False  # exact share of the whole data set
        return mt.ci_low is not None or mt.p_value is not None or mt.unit in ("ratio", "correlation", "days")

    thin = [mid for mid in f.evidence if mid in metrics and metrics[mid].n < min_sample and is_estimate(mid)]
    if thin:
        caveat_text = (" ".join(f.caveats) + " " + f.claim).lower()
        if not any(w in caveat_text for w in _SMALL_SAMPLE_WORDS):
            r.errors.append(f"relies on small samples (n<{min_sample}) without a caveat: {', '.join(thin)}")
        else:
            r.warnings.append(f"small samples: {', '.join(thin)}")

    cited = set(f.evidence) | {q.metric_id for q in f.quoted_numbers}
    caveat_text = " ".join(f.caveats).lower() + " " + f.claim.lower()
    significant_cited = [mid for mid in cited if mid in metrics and metrics[mid].significant]

    # Comparisons between groups need a test or an honest "could be chance".
    families: dict[tuple[str, str], set[str]] = {}
    for mid in cited:
        mm = _GROUP_METRIC.match(mid)
        dim = mm.group(1).replace(".", "_") if mm else None
        if mm and mm.group(3) in TESTED.get(dim, ()):
            families.setdefault((dim, mm.group(3)), set()).add(mm.group(2))
    for (dim, measure), keys in families.items():
        if len(keys) < 2:
            continue
        tests = [f"test.{dim}.{k}.{measure}" for k in keys]
        if any(t in cited and metrics.get(t) and metrics[t].significant for t in tests):
            continue
        if any(w in caveat_text for w in _CHANCE_WORDS):
            continue
        r.errors.append(f"compares {dim} {measure} across {', '.join(sorted(keys))} without a significant "
                        f"test.{dim}.*.{measure} metric or a caveat that the difference could be chance")

    if _SIGNIF_CLAIM.search(f.claim) and not significant_cited \
            and not re.search(r"not (statistically )?significant", f.claim, re.I):
        r.errors.append("claims significance but cites no metric marked significant")

    r.passed = not r.errors
    return r


def check_all(findings: list[Finding], metrics: list[Metric], min_sample: int, tol: float) -> list[CheckResult]:
    by_id = {m.id: m for m in metrics}
    return [check_finding(f, by_id, min_sample, tol) for f in findings]
