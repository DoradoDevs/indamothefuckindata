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
  lifecycle_stage TEXT, source TEXT, campaign_id TEXT, company_id TEXT
);
CREATE TABLE companies (
  company_id TEXT PRIMARY KEY, name TEXT, industry TEXT, state TEXT, created_at TEXT
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


def build_db(raw: dict[str, list[dict]], db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)

    con.executemany("INSERT INTO contacts VALUES (?,?,?,?,?,?,?)", [
        (c["id"], (ca := _dt(p.get("createdate"))), _month(ca), p.get("lifecyclestage"),
         p.get("hs_analytics_source"), p.get("hs_analytics_first_touch_converting_campaign"),
         _first(c.get("associations"), "companies"))
        for c in raw.get("contacts", []) for p in [c.get("properties", {})]
    ])
    con.executemany("INSERT INTO companies VALUES (?,?,?,?,?)", [
        (c["id"], p.get("name"), p.get("industry"), p.get("state"), _dt(p.get("createdate")))
        for c in raw.get("companies", []) for p in [c.get("properties", {})]
    ])
    rows = []
    for d in raw.get("deals", []):
        p = d.get("properties", {})
        stage = (p.get("dealstage") or "").lower()
        closed_at = _dt(p.get("closedate"))
        is_won = stage == "closedwon"
        is_closed = is_won or stage == "closedlost"
        rows.append((d["id"], _num(p.get("amount")), stage, p.get("pipeline"), _dt(p.get("createdate")),
                     closed_at, _month(closed_at) if is_closed else None, int(is_closed), int(is_won),
                     _first(d.get("associations"), "contacts"), _first(d.get("associations"), "companies")))
    con.executemany("INSERT INTO deals VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
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
    con.commit()
    return con
