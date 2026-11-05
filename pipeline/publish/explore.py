"""On-demand queries behind the dashboard's date-range and grouping controls.

Two kinds of numbers, kept deliberately separate:

* Activity in a period - what happened in those months (contacts created, deals won
  and closed, won value). Used for the headline tiles.
* Results from people who came in during a period ("cohort") - contacts created in
  those months and every deal they have produced since, whenever it closed. Used to
  compare campaigns, sources, and industries fairly.

Cohort numbers reuse metrics.compute.compute() on a filtered copy of the warehouse, so
the dashboard and Claude's findings share one set of metric definitions.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from pipeline.metrics.compute import compute

MAX_SERIES = 8  # categorical palette size; extra groups fold into "Other"

GROUP_EXPR = {
    "none": "'All'",
    "campaign": "COALESCE(c.campaign_id, 'none')",
    "source": "COALESCE(c.source, 'UNKNOWN')",
    "industry": "COALESCE(co.industry, 'Unknown')",
}


def _open(warehouse: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{warehouse}?mode=ro", uri=True)


def options(warehouse: Path) -> dict:
    con = _open(warehouse)
    months = [r[0] for r in con.execute(
        """SELECT m FROM (SELECT created_month m FROM contacts UNION SELECT closed_month FROM deals
                          UNION SELECT month FROM web_traffic) WHERE m IS NOT NULL ORDER BY m""")]
    campaigns = [{"id": cid, "name": name, "channel": ch} for cid, name, ch in
                 con.execute("SELECT campaign_id, name, channel FROM campaigns ORDER BY campaign_id")]
    campaigns.append({"id": "none", "name": "No campaign", "channel": None})
    sources = [r[0] for r in con.execute("SELECT DISTINCT COALESCE(source,'UNKNOWN') FROM contacts ORDER BY 1")]
    industries = [r[0] for r in con.execute("SELECT DISTINCT COALESCE(industry,'Unknown') FROM companies ORDER BY 1")]
    has_traffic = con.execute("SELECT COUNT(*) FROM web_traffic").fetchone()[0] > 0
    return {"months": months, "campaigns": campaigns, "sources": sources, "industries": industries,
            "has_traffic": has_traffic}


def _shift(month: str, n: int) -> str:
    y, m = int(month[:4]), int(month[5:7]) + n
    while m <= 0:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return f"{y:04d}-{m:02d}"


def previous_window(start: str, end: str) -> tuple[str, str]:
    length = (int(end[:4]) - int(start[:4])) * 12 + int(end[5:7]) - int(start[5:7]) + 1
    return _shift(start, -length), _shift(start, -1)


def activity(warehouse: Path, start: str, end: str) -> dict:
    con = _open(warehouse)
    q = lambda sql: con.execute(sql, (start, end)).fetchone()  # noqa: E731
    contacts = q("SELECT COUNT(*) FROM contacts WHERE created_month BETWEEN ? AND ?")[0]
    deals = q("SELECT COUNT(*) FROM deals WHERE substr(created_at,1,7) BETWEEN ? AND ?")[0]
    closed, won, value = q("""SELECT COUNT(*), COALESCE(SUM(is_won),0),
                                     COALESCE(SUM(CASE WHEN is_won=1 THEN amount END),0)
                              FROM deals WHERE closed_month BETWEEN ? AND ?""")
    sessions = q("SELECT SUM(sessions) FROM web_traffic WHERE month BETWEEN ? AND ?")[0]
    return {
        "new_contacts": contacts, "deals_created": deals, "deals_won": won, "deals_closed": closed,
        "won_value": float(value), "win_rate": (won / closed) if closed else None, "sessions": sessions,
    }


def cohort_metrics(warehouse: Path, start: str, end: str, prefixes: tuple[str, ...]) -> list[dict]:
    mem = sqlite3.connect(":memory:")
    src = _open(warehouse)
    src.backup(mem)
    src.close()
    mem.executescript("""
        CREATE TEMP TABLE keep_c AS SELECT contact_id FROM main.contacts
            WHERE created_month BETWEEN '{s}' AND '{e}';
        DELETE FROM main.contacts WHERE contact_id NOT IN (SELECT contact_id FROM keep_c);
        DELETE FROM main.deals WHERE contact_id IS NULL OR contact_id NOT IN (SELECT contact_id FROM keep_c);
        DELETE FROM main.web_traffic WHERE month NOT BETWEEN '{s}' AND '{e}';
        DELETE FROM main.email_stats WHERE substr(send_date,1,7) NOT BETWEEN '{s}' AND '{e}';
    """.format(s=_month_or_fail(start), e=_month_or_fail(end)))
    return [m.to_dict() for m in compute(mem) if m.id.startswith(prefixes)]


def _month_or_fail(m: str) -> str:
    if len(m) != 7 or m[4] != "-" or not (m[:4] + m[5:]).isdigit():
        raise ValueError(f"bad month: {m}")
    return m


MEASURES = {
    # measure: (month column, FROM/WHERE clause, value expression)
    "new_contacts": ("c.created_month", "contacts c LEFT JOIN companies co ON co.company_id = c.company_id", "COUNT(*)"),
    "deals_created": ("substr(d.created_at,1,7)",
                      "deals d LEFT JOIN contacts c ON c.contact_id = d.contact_id "
                      "LEFT JOIN companies co ON co.company_id = d.company_id", "COUNT(*)"),
    "deals_won": ("d.closed_month",
                  "deals d LEFT JOIN contacts c ON c.contact_id = d.contact_id "
                  "LEFT JOIN companies co ON co.company_id = d.company_id WHERE d.is_won = 1", "COUNT(*)"),
    "won_value": ("d.closed_month",
                  "deals d LEFT JOIN contacts c ON c.contact_id = d.contact_id "
                  "LEFT JOIN companies co ON co.company_id = d.company_id WHERE d.is_won = 1",
                  "COALESCE(SUM(d.amount),0)"),
}


def series(warehouse: Path, measure: str, by: str, start: str, end: str) -> dict:
    """Monthly series, one per group. Groups past MAX_SERIES fold into 'Other'."""
    start, end = _month_or_fail(start), _month_or_fail(end)
    con = _open(warehouse)
    months = []
    m = start
    while m <= end:
        months.append(m)
        m = _shift(m, 1)

    if measure == "sessions":
        group = "source" if by == "source" else "'All'"
        rows = con.execute(f"""SELECT {group}, month, SUM(sessions) FROM web_traffic
                               WHERE month BETWEEN ? AND ? GROUP BY 1, 2""", (start, end)).fetchall()
        if by not in ("none", "source"):
            by = "none"
    else:
        if measure not in MEASURES or by not in GROUP_EXPR:
            raise ValueError("bad measure or grouping")
        month_col, frm, val = MEASURES[measure]
        joiner = " AND " if " WHERE " in frm else " WHERE "
        rows = con.execute(f"""SELECT {GROUP_EXPR[by]} g, {month_col} mo, {val} FROM {frm}
                               {joiner} {month_col} BETWEEN ? AND ? GROUP BY g, mo""", (start, end)).fetchall()

    by_group: dict[str, dict[str, float]] = {}
    for g, mo, v in rows:
        by_group.setdefault(g, {})[mo] = float(v or 0)
    totals = sorted(by_group, key=lambda g: -sum(by_group[g].values()))
    keep, rest = totals[:MAX_SERIES - 1], totals[MAX_SERIES - 1:]
    if len(totals) <= MAX_SERIES:
        keep, rest = totals, []
    out = [{"key": g, "values": [by_group[g].get(mo, 0.0) for mo in months]} for g in keep]
    if rest:
        out.append({"key": "Other", "values": [sum(by_group[g].get(mo, 0.0) for g in rest) for mo in months]})
    return {"measure": measure, "by": by, "months": months, "series": out}
