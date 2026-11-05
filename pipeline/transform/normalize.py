"""Raw HubSpot-shaped JSON -> tidy SQLite tables, with PII removed.

Only allow-listed fields are copied out of each record, so any personal field that
shows up in the raw data (names, emails, phones, addresses, or anything new HubSpot
adds) is dropped by default rather than by a deny-list someone has to maintain.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE contacts (
  contact_id TEXT PRIMARY KEY, created_at TEXT, created_month TEXT,
  lifecycle_stage TEXT, source TEXT, campaign_id TEXT, company_id TEXT,
  email_hash TEXT  -- keyed one-way hash, for Salesforce matching only; never displayed
);
CREATE TABLE companies (
  company_id TEXT PRIMARY KEY, name TEXT, domain TEXT, industry TEXT, state TEXT, created_at TEXT
);
CREATE TABLE deals (
  deal_id TEXT PRIMARY KEY, amount REAL, stage TEXT, pipeline TEXT,
  created_at TEXT, closed_at TEXT, closed_month TEXT,
  is_closed INTEGER, is_won INTEGER, contact_id TEXT, company_id TEXT
);
CREATE TABLE campaigns (
  campaign_id TEXT PRIMARY KEY, name TEXT, channel TEXT, start_date TEXT, end_date TEXT
);
CREATE TABLE email_stats (
  email_id TEXT PRIMARY KEY, campaign_id TEXT, send_date TEXT,
  sent INTEGER, delivered INTEGER, opened INTEGER, clicked INTEGER
);
CREATE TABLE web_traffic (month TEXT, source TEXT, sessions INTEGER);
CREATE TABLE pipelines (pipeline_id TEXT PRIMARY KEY, label TEXT);
"""

# Company "name" is a business name, not personal data; kept for internal drill-down
# but never sent to the agent pack. Revisit if sole proprietors appear in HubSpot.


def _dt(v: str | None) -> str | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).isoformat()
    except ValueError:
        # HubSpot sometimes returns epoch millis for date properties.
        try:
            return datetime.fromtimestamp(int(v) / 1000).isoformat()
        except (ValueError, OSError):
            return None


def _month(iso: str | None) -> str | None:
    return iso[:7] if iso else None


def _num(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _first(assoc: dict | None, key: str) -> str | None:
    ids = (assoc or {}).get(key) or []
    return str(ids[0]) if ids else None


def _domain(v: str | None) -> str | None:
    if not v:
        return None
    d = v.strip().lower()
    for prefix in ("https://", "http://"):
        d = d.removeprefix(prefix)
    d = d.removeprefix("www.").split("/")[0]
    return d or None


def _bool(v) -> bool | None:
    if v in (None, ""):
        return None
    return str(v).lower() == "true"


def build_db(raw: dict[str, list[dict]], db_path: Path,
             exclude_contact_ids: frozenset[str] = frozenset()) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)

    # VERIFY: HubSpot's first-touch campaign property may hold the campaign's ID or its
    # name depending on account setup; resolve either to the campaign ID.
    campaign_by_name = {(c.get("properties") or {}).get("hs_name", "").strip().lower(): str(c["id"])
                        for c in raw.get("campaigns", []) if (c.get("properties") or {}).get("hs_name")}
    campaign_ids = {str(c["id"]) for c in raw.get("campaigns", [])}

    def resolve_campaign(v):
        if not v:
            return None
        v = str(v)
        if v in campaign_ids or not campaign_by_name:
            return v
        return campaign_by_name.get(v.strip().lower(), v)

    seen: set[str] = set()
    contact_rows = []
    for c in raw.get("contacts", []):
        cid = str(c["id"])
        if cid in exclude_contact_ids or cid in seen:
            continue
        seen.add(cid)
        p = c.get("properties", {})
        ca = _dt(p.get("createdate"))
        contact_rows.append((cid, ca, _month(ca), p.get("lifecyclestage"), p.get("hs_analytics_source"),
                             resolve_campaign(p.get("hs_analytics_first_touch_converting_campaign")),
                             _first(c.get("associations"), "companies"), p.get("email_hash")))
    con.executemany("INSERT INTO contacts VALUES (?,?,?,?,?,?,?,?)", contact_rows)
    con.executemany("INSERT OR IGNORE INTO companies VALUES (?,?,?,?,?,?)", [
        (str(c["id"]), p.get("name"), _domain(p.get("domain")), p.get("industry"), p.get("state"),
         _dt(p.get("createdate")))
        for c in raw.get("companies", []) for p in [c.get("properties", {})]
    ])
    rows = []
    for d in raw.get("deals", []):
        p = d.get("properties", {})
        stage = (p.get("dealstage") or "").lower()
        closed_at = _dt(p.get("closedate"))
        # Prefer HubSpot's own flags; stage IDs aren't reliable across pipelines.
        is_won = _bool(p.get("hs_is_closed_won"))
        if is_won is None:
            is_won = stage == "closedwon"
        is_closed = _bool(p.get("hs_is_closed"))
        if is_closed is None:
            is_closed = is_won or stage == "closedlost"
        amount = _num(p.get("amount_in_home_currency"))
        if amount is None:
            amount = _num(p.get("amount"))
        rows.append((str(d["id"]), amount, stage, p.get("pipeline"), _dt(p.get("createdate")),
                     closed_at, _month(closed_at) if is_closed else None, int(is_closed), int(is_won),
                     _first(d.get("associations"), "contacts"), _first(d.get("associations"), "companies")))
    con.executemany("INSERT OR IGNORE INTO deals VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO campaigns VALUES (?,?,?,?,?)", [
        (c["id"], p.get("hs_name"), p.get("channel"), p.get("hs_start_date"), p.get("hs_end_date"))
        for c in raw.get("campaigns", []) for p in [c.get("properties", {})]
    ])
    con.executemany("INSERT INTO email_stats VALUES (?,?,?,?,?,?,?)", [
        (e["email_id"], e.get("campaign_id"), e.get("send_date"), e.get("sent"), e.get("delivered"),
         e.get("opened"), e.get("clicked"))
        for e in raw.get("email_stats", [])
    ])
    con.executemany("INSERT INTO web_traffic VALUES (?,?,?)", [
        (w["month"][:7], w.get("source"), w.get("sessions")) for w in raw.get("web_traffic", [])
    ])
    con.executemany("INSERT INTO pipelines VALUES (?,?)", [
        (pl["id"], pl.get("label")) for pl in raw.get("pipelines", [])])
    con.commit()
    return con
