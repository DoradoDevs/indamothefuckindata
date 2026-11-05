"""Marketing -> service deal metrics (HubSpot marketing matched to Salesforce opportunities).

Two kinds of numbers, matching the rest of the dashboard:
  * service.overview.*   activity: opportunities closed (won/lost) in the window.
  * service.campaign.* / service.source.*   cohort: HubSpot contacts who came in during
    the window and the service deals credited to them, whenever those closed.
Only "credited" deals count toward campaigns/sources: the HubSpot contact existed
before the opportunity was created.
"""
from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime

from pipeline.metrics import stats
from pipeline.metrics.compute import Metric, _Collector, _mean_tests, _proportion_tests, _ratio

ALL = ("0000-00", "9999-99")


def has_salesforce(con: sqlite3.Connection) -> bool:
    try:
        return con.execute("SELECT COUNT(*) FROM service_deals").fetchone()[0] > 0
    except sqlite3.OperationalError:
        return False


def compute_service(con: sqlite3.Connection, start: str = ALL[0], end: str = ALL[1]) -> list[Metric]:
    if not has_salesforce(con):
        return []
    m = _Collector()
    w = (start, end)

    # --- Activity in the window ---------------------------------------------
    n_closed, n_won, value = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(is_won),0), COALESCE(SUM(CASE WHEN is_won=1 THEN amount END),0) "
        "FROM service_deals WHERE closed_month BETWEEN ? AND ?", w).fetchone()
    n_created = con.execute("SELECT COUNT(*) FROM service_deals WHERE substr(created_at,1,7) BETWEEN ? AND ?",
                            w).fetchone()[0]
    matched, credited = con.execute(
        "SELECT COALESCE(SUM(match_method != 'none'),0), COALESCE(SUM(credited),0) FROM service_deals "
        "WHERE closed_month BETWEEN ? AND ? AND is_won = 1", w).fetchone()
    m.add("service.overview.opportunities_created", n_created, "count", n_created, "Service opportunities created")
    m.add("service.overview.deals_won", n_won, "count", n_closed, "Service deals won (closed in period)")
    m.add("service.overview.won_value", float(value), "usd", n_won, "Value of service deals won (closed in period)")
    m.add("service.overview.win_rate", _ratio(n_won, n_closed), "ratio", n_closed,
          "Won / closed service deals", ci=stats.wilson(n_won, n_closed))
    m.add("service.overview.won_matched_rate", _ratio(matched, n_won), "ratio", n_won,
          "Share of won service deals matched to a HubSpot contact or company", ci=stats.wilson(matched, n_won))
    m.add("service.overview.won_credited_rate", _ratio(credited, n_won), "ratio", n_won,
          "Share of won service deals where HubSpot marketing reached the customer before the deal started",
          ci=stats.wilson(credited, n_won))

    # --- Cohort: contacts created in the window, by campaign and source -------
    names = {cid: name for cid, name in con.execute("SELECT campaign_id, name FROM campaigns")}
    for dim, col in (("campaign", "campaign_id"), ("source", "source")):
        rows = con.execute(f"""
            SELECT COALESCE(c.{col}, 'none') k, c.contact_id,
                   COUNT(sd.opp_id), COALESCE(SUM(sd.is_closed),0), COALESCE(SUM(sd.is_won),0),
                   COALESCE(SUM(CASE WHEN sd.is_won=1 THEN sd.amount END),0)
            FROM contacts c LEFT JOIN service_deals sd ON sd.hs_contact_id = c.contact_id AND sd.credited = 1
            WHERE c.created_month BETWEEN ? AND ?
            GROUP BY k, c.contact_id""", w).fetchall()
        groups: dict[str, dict] = {}
        for k, _cid, opps, closed, won, val in rows:
            g = groups.setdefault(k, {"contacts": 0, "with_won": 0, "opps": 0, "closed": 0, "won": 0, "value": [],
                                      })
            g["contacts"] += 1
            g["with_won"] += 1 if won else 0
            g["opps"] += opps
            g["closed"] += closed
            g["won"] += won
            g["value"].append(float(val))
        label = (lambda k: names.get(k, k)) if dim == "campaign" else (lambda k: f"source {k}")
        for k, g in sorted(groups.items()):
            p = f"service.{dim}.{k}"
            lab = "No campaign" if k == "none" and dim == "campaign" else label(k)
            m.add(f"{p}.contacts", g["contacts"], "count", g["contacts"], f"HubSpot contacts, {lab}")
            m.add(f"{p}.deals_won", g["won"], "count", g["opps"], f"Credited service deals won, {lab}")
            m.add(f"{p}.won_value", float(sum(g["value"])), "usd", g["closed"], f"Credited service value won, {lab}")
            m.add(f"{p}.contact_to_service_win_rate", _ratio(g["with_won"], g["contacts"]), "ratio", g["contacts"],
                  f"Share of {lab} contacts who went on to a won service deal",
                  ci=stats.wilson(g["with_won"], g["contacts"]))
            m.add(f"{p}.service_value_per_contact", statistics.fmean(g["value"]) if g["value"] else None, "usd",
                  g["contacts"], f"Credited service value won per contact, {lab}", ci=stats.mean_ci(g["value"]))
        display = {k: ("No campaign" if k == "none" and dim == "campaign" else label(k)) for k in groups}
        _proportion_tests(m, f"service_{dim}", "contact_to_service_win_rate", "Share of contacts with a won service deal",
                          {k: (g["with_won"], g["contacts"]) for k, g in groups.items()}, display)
        _mean_tests(m, f"service_{dim}", "service_value_per_contact", "Service value per contact",
                    {k: g["value"] for k, g in groups.items()}, display)

    # --- Time from first marketing touch to won service deal -----------------
    days = []
    for a, b in con.execute("""SELECT c.created_at, sd.closed_at FROM service_deals sd
                               JOIN contacts c ON c.contact_id = sd.hs_contact_id
                               WHERE sd.credited = 1 AND sd.is_won = 1 AND sd.closed_month BETWEEN ? AND ?""", w):
        if a and b:
            days.append((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400)
    if days:
        m.add("service.overview.median_days_first_touch_to_won", statistics.median(days), "days", len(days),
              "Median days from first HubSpot touch to won service deal")

    unmatched = con.execute("SELECT COUNT(*) FROM service_deals WHERE match_method = 'none'").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM service_deals").fetchone()[0]
    m.add("quality.service_deals_unmatched_rate", _ratio(unmatched, total), "ratio", total,
          "Share of Salesforce opportunities with no HubSpot contact or company match")
    return m.items
