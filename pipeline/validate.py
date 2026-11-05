"""Pull validation: sanity checks on every run, before any analysis.

The first real pull will surface surprises (property names, empty fields, scopes).
This turns them into a readable report instead of quietly wrong numbers.

Levels: error (analysis is skipped unless --force), warning (shown on Data health),
info (context).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def validate(con: sqlite3.Connection, raw: dict) -> dict:
    checks: list[dict] = []

    def add(id_: str, level: str, message: str, value=None):
        checks.append({"id": id_, "level": level, "message": message, "value": value})

    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    n_contacts, n_deals, n_companies = q("SELECT COUNT(*) FROM contacts"), q("SELECT COUNT(*) FROM deals"), \
        q("SELECT COUNT(*) FROM companies")

    for name, n in (("contacts", n_contacts), ("companies", n_companies), ("deals", n_deals)):
        add(f"count.{name}", "info", f"{n:,} {name} loaded", n)
    if n_contacts == 0:
        add("count.contacts.zero", "error", "No contacts loaded. Check the token's scopes and the extract log.")

    raw_contacts = len(raw.get("contacts", []))
    if raw_contacts and raw_contacts != n_contacts:
        add("dedupe.contacts", "info", f"{raw_contacts - n_contacts:,} contact records dropped "
            "(duplicates or excluded IDs)", raw_contacts - n_contacts)

    bad_dates = q("SELECT COUNT(*) FROM contacts WHERE created_at IS NULL")
    r = _rate(bad_dates, n_contacts)
    if r is not None and r > 0.05:
        add("dates.contacts", "error", f"{r:.0%} of contacts have no readable create date. "
            "Date parsing or property names need fixing before analysis.", r)

    future = q(f"SELECT COUNT(*) FROM contacts WHERE created_at > '{(datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}'")
    if future:
        add("dates.future", "warning", f"{future:,} contacts have a create date in the future", future)

    fills = [
        ("fill.source", "SELECT COUNT(*) FROM contacts WHERE source IS NULL", n_contacts, 0.5,
         "contacts have no original source; lead source results won't cover them"),
        ("fill.campaign", "SELECT COUNT(*) FROM contacts WHERE campaign_id IS NULL", n_contacts, 0.8,
         "contacts have no first-touch campaign; campaign results will be thin"),
        ("fill.company", "SELECT COUNT(*) FROM contacts WHERE company_id IS NULL", n_contacts, 0.5,
         "contacts aren't linked to a company; industry results won't cover them"),
        ("fill.industry", "SELECT COUNT(*) FROM companies WHERE industry IS NULL", n_companies, 0.5,
         "companies have no industry"),
        ("fill.amount", "SELECT COUNT(*) FROM deals WHERE amount IS NULL", n_deals, 0.1,
         "deals have no amount; won value is understated"),
        ("fill.deal_contact", "SELECT COUNT(*) FROM deals WHERE contact_id IS NULL", n_deals, 0.2,
         "deals have no contact; they can't be credited to a campaign or source"),
    ]
    for id_, sql, den, threshold, text in fills:
        missing = q(sql)
        rate = _rate(missing, den)
        if rate is None:
            continue
        level = "warning" if rate > threshold else "info"
        add(id_, level, f"{rate:.0%} of {text}", round(rate, 4))

    if n_deals:
        won_no_close = q("SELECT COUNT(*) FROM deals WHERE is_won = 1 AND closed_at IS NULL")
        if won_no_close:
            add("deals.won_no_closedate", "warning", f"{won_no_close:,} won deals have no close date; "
                "they're missing from monthly trends", won_no_close)
        unknown_pl = q("SELECT COUNT(*) FROM deals d LEFT JOIN pipelines p ON p.pipeline_id = d.pipeline "
                       "WHERE p.pipeline_id IS NULL")
        if unknown_pl and q("SELECT COUNT(*) FROM pipelines"):
            add("deals.unknown_pipeline", "warning", f"{unknown_pl:,} deals are in a pipeline the API didn't list",
                unknown_pl)
        orphan = q("SELECT COUNT(*) FROM deals d WHERE d.contact_id IS NOT NULL AND d.contact_id NOT IN "
                   "(SELECT contact_id FROM contacts)")
        if orphan:
            add("deals.orphan_contact", "info", f"{orphan:,} deals point to contacts not in scope "
                "(excluded or archived)", orphan)
    if raw.get("campaigns"):
        unmatched = q("SELECT COUNT(*) FROM contacts WHERE campaign_id IS NOT NULL AND campaign_id NOT IN "
                      "(SELECT campaign_id FROM campaigns)")
        if unmatched:
            add("campaign.unmatched", "warning", f"{unmatched:,} contacts reference a campaign that isn't in the "
                "campaign list (VERIFY the campaign property format)", unmatched)

    hashed = q("SELECT COUNT(*) FROM contacts WHERE email_hash IS NOT NULL")
    add("fill.email_hash", "info", f"{hashed:,} contacts can be matched to Salesforce by email", hashed)

    try:
        n_opps = q("SELECT COUNT(*) FROM service_deals")
    except sqlite3.OperationalError:
        n_opps = None
    if n_opps:
        add("count.sf_opportunities", "info", f"{n_opps:,} Salesforce opportunities loaded", n_opps)
        unmatched = q("SELECT COUNT(*) FROM service_deals WHERE match_method = 'none'")
        r = unmatched / n_opps
        add("sf.unmatched", "warning" if r > 0.5 else "info",
            f"{r:.0%} of Salesforce opportunities couldn't be matched to HubSpot (by contact email or company "
            "domain); marketing can't be credited for them", round(r, 4))
        by_contact = q("SELECT COUNT(*) FROM service_deals WHERE match_method = 'contact'")
        by_account = q("SELECT COUNT(*) FROM service_deals WHERE match_method = 'account'")
        add("sf.by_contact", "info", f"{by_contact:,} opportunities matched to a HubSpot contact by email, "
            f"{by_account:,} by company domain only", by_contact)
    elif n_opps == 0:
        add("count.sf_opportunities.zero", "warning", "Salesforce returned no opportunities. Check the "
            "integration user's access to Opportunity.")

    for w in raw.get("_warnings", []):
        add("extract.warning", "warning", w)

    level_rank = {"info": 0, "warning": 1, "error": 2}
    worst = max((level_rank[c["level"]] for c in checks), default=0)
    return {"status": ["ok", "warning", "error"][worst], "checks": checks}
