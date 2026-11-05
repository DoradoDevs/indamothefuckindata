"""Salesforce records -> warehouse tables, matched to HubSpot.

Matching (in order of confidence):
  1. contact: an Opportunity Contact Role -> Salesforce contact -> HubSpot contact with
     the same keyed email hash. Primary contact roles are tried first.
  2. account: no contact match -> Salesforce account -> HubSpot company with the same
     web domain -> the earliest-created HubSpot contact at that company.

Marketing gets credit for a deal ("credited") only if the matched HubSpot contact was
created before the opportunity. Deals that started first are kept but not credited.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pipeline.transform.normalize import _domain

SCHEMA = """
CREATE TABLE sf_accounts (account_id TEXT PRIMARY KEY, domain TEXT, industry TEXT, state TEXT, type TEXT);
CREATE TABLE sf_contacts (sf_contact_id TEXT PRIMARY KEY, account_id TEXT, email_hash TEXT, created_at TEXT);
CREATE TABLE sf_opportunities (
  opp_id TEXT PRIMARY KEY, account_id TEXT, amount REAL, stage TEXT, is_closed INTEGER, is_won INTEGER,
  created_at TEXT, closed_at TEXT, closed_month TEXT, service_type TEXT
);
CREATE TABLE sf_opp_contacts (opp_id TEXT, sf_contact_id TEXT, is_primary INTEGER);
CREATE TABLE service_deals (
  opp_id TEXT PRIMARY KEY, match_method TEXT, hs_contact_id TEXT, hs_company_id TEXT,
  credited INTEGER, amount REAL, is_closed INTEGER, is_won INTEGER,
  created_at TEXT, closed_at TEXT, closed_month TEXT, service_type TEXT
);
"""


def _dt(v) -> str | None:
    """Salesforce datetimes look like 2026-01-05T14:00:00.000+0000; dates like 2026-01-05."""
    if not v:
        return None
    s = str(v).replace("Z", "+00:00")
    if len(s) > 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat()


def build_sf(con: sqlite3.Connection, raw: dict) -> dict:
    con.executescript(SCHEMA)
    con.executemany("INSERT OR IGNORE INTO sf_accounts VALUES (?,?,?,?,?)", [
        (a["Id"], _domain(a.get("Website")), a.get("Industry"), a.get("BillingState"), a.get("Type"))
        for a in raw.get("sf_accounts", [])])
    con.executemany("INSERT OR IGNORE INTO sf_contacts VALUES (?,?,?,?)", [
        (c["Id"], c.get("AccountId"), c.get("EmailHash"), _dt(c.get("CreatedDate")))
        for c in raw.get("sf_contacts", [])])
    rows = []
    for o in raw.get("sf_opportunities", []):
        closed_at = _dt(o.get("CloseDate"))
        is_closed = bool(o.get("IsClosed"))
        amount = o.get("Amount")
        rows.append((o["Id"], o.get("AccountId"), float(amount) if amount not in (None, "") else None,
                     o.get("StageName"), int(is_closed), int(bool(o.get("IsWon"))), _dt(o.get("CreatedDate")),
                     closed_at, closed_at[:7] if closed_at and is_closed else None, o.get("Type")))
    con.executemany("INSERT OR IGNORE INTO sf_opportunities VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO sf_opp_contacts VALUES (?,?,?)", [
        (r["OpportunityId"], r["ContactId"], int(bool(r.get("IsPrimary"))))
        for r in raw.get("sf_opp_contacts", [])])

    # Lookups
    hs_by_hash = {}
    for cid, h, created in con.execute(
            "SELECT contact_id, email_hash, created_at FROM contacts WHERE email_hash IS NOT NULL ORDER BY created_at"):
        hs_by_hash.setdefault(h, (cid, created))
    sf_hash = dict(con.execute("SELECT sf_contact_id, email_hash FROM sf_contacts"))
    roles: dict[str, list[str]] = {}
    for opp, sfc, _p in con.execute("SELECT opp_id, sf_contact_id, is_primary FROM sf_opp_contacts "
                                    "ORDER BY is_primary DESC"):
        roles.setdefault(opp, []).append(sfc)
    company_by_domain = {}
    for co, dom in con.execute("SELECT company_id, domain FROM companies WHERE domain IS NOT NULL"):
        company_by_domain.setdefault(dom, co)
    acct_domain = dict(con.execute("SELECT account_id, domain FROM sf_accounts"))
    earliest_at_company = {}
    for co, cid, created in con.execute("SELECT company_id, contact_id, created_at FROM contacts "
                                        "WHERE company_id IS NOT NULL ORDER BY created_at"):
        earliest_at_company.setdefault(co, (cid, created))

    out, stats = [], {"contact": 0, "account": 0, "none": 0}
    for opp_id, acct, amount, is_closed, is_won, created, closed_at, closed_month, svc in con.execute(
            "SELECT opp_id, account_id, amount, is_closed, is_won, created_at, closed_at, closed_month, service_type "
            "FROM sf_opportunities"):
        method, hs_contact, hs_created = "none", None, None
        for sfc in roles.get(opp_id, []):
            hit = hs_by_hash.get(sf_hash.get(sfc))
            if hit:
                method, (hs_contact, hs_created) = "contact", hit
                break
        hs_company = company_by_domain.get(acct_domain.get(acct)) if acct else None
        if method == "none" and hs_company and hs_company in earliest_at_company:
            method, (hs_contact, hs_created) = "account", earliest_at_company[hs_company]
        credited = int(bool(hs_contact and hs_created and created and hs_created <= created))
        stats[method] += 1
        out.append((opp_id, method, hs_contact, hs_company, credited, amount, is_closed, is_won,
                    created, closed_at, closed_month, svc))
    con.executemany("INSERT INTO service_deals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", out)
    con.commit()
    return stats
