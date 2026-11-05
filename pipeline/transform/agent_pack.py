"""Builds the compact bundle Claude reads: metrics + data-quality context.

Claude never sees the database or raw records - only aggregates, which keeps prompts
small, keeps PII out, and means every number Claude can see has a metric ID.
"""
from __future__ import annotations

import json
import sqlite3

from pipeline.metrics.compute import Metric


def _fmt(v, unit: str) -> str:
    if v is None:
        return "n/a"
    if unit == "usd":
        return f"${v:,.2f}"
    if unit in ("ratio",):
        return f"{v:.4f}"
    if unit == "correlation":
        return f"{v:+.4f}"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return f"{v:,}"


def build(con: sqlite3.Connection, metrics: list[Metric], previous: dict[str, float] | None = None) -> str:
    lo, hi = con.execute("SELECT MIN(created_month), MAX(created_month) FROM contacts").fetchone()
    campaigns = con.execute("SELECT campaign_id, name, channel, start_date FROM campaigns ORDER BY 1").fetchall()

    lines = [
        "# HubSpot analytics agent pack",
        "",
        f"Data window (contact created month): {lo} to {hi}.",
        "Revenue = sum of `amount` on closed-won deals. Attribution = contact's first-touch converting campaign.",
        "Deals are attributed through their first associated contact.",
        "Recent months undercount revenue because many of their deals are still open.",
        "",
        "## Campaigns",
        "",
        "| campaign_id | name | channel | start |",
        "|---|---|---|---|",
        *[f"| {c[0]} | {c[1]} | {c[2] or ''} | {c[3] or ''} |" for c in campaigns],
        "",
        "## Metrics",
        "",
        "Ratios are fractions (0.25 = 25%). `n` is the sample size the metric rests on.",
        "`95% range` is the plausible range for the true value. `q` is a p-value adjusted for",
        "testing many groups at once; `sig` = yes means the difference is unlikely to be chance.",
        "`test.<group>.<key>.<measure>` = that group's value minus all other groups combined.",
        "",
        "| id | value | unit | n | 95% range | q | sig | description |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for mt in metrics:
        rng = f"{_fmt(mt.ci_low, mt.unit)} to {_fmt(mt.ci_high, mt.unit)}" if mt.ci_low is not None else ""
        q = f"{mt.p_value:.4f}" if mt.p_value is not None else ""
        sig = "" if mt.significant is None else ("yes" if mt.significant else "no")
        lines.append(f"| {mt.id} | {_fmt(mt.value, mt.unit)} | {mt.unit} | {mt.n} | {rng} | {q} | {sig} | {mt.description} |")

    if previous:
        deltas = []
        for mt in metrics:
            if mt.id.startswith(("overview.", "campaign.", "source.")) and mt.id in previous \
                    and mt.value is not None and previous[mt.id] not in (None, 0):
                change = (mt.value - previous[mt.id]) / abs(previous[mt.id])
                if abs(change) >= 0.10:
                    deltas.append(f"| {mt.id} | {_fmt(previous[mt.id], mt.unit)} | {_fmt(mt.value, mt.unit)} | {change:+.1%} |")
        if deltas:
            lines += ["", "## Changes since previous run (>=10%)", "",
                      "| id | previous | current | change |", "|---|---|---|---|", *deltas]
    return "\n".join(lines) + "\n"


def metrics_json(metrics: list[Metric]) -> str:
    return json.dumps([m.to_dict() for m in metrics], indent=2)
