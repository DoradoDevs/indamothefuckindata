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
  email_hash TEXT,  -- keyed one-way hash, for Salesforce matching only; never displayed
  email_opened INTEGER, email_clicked INTEGER, form_submitted INTEGER, first_form TEXT, web_visits INTEGER,
  email_delivered INTEGER, email_open_count INTEGER, email_click_count INTEGER, last_open TEXT, last_click TEXT,
  opted_out INTEGER, sales_activities INTEGER, last_contacted TEXT, owner_id TEXT,
  source_detail_1 TEXT, source_detail_2 TEXT, recent_form TEXT, recent_form_at TEXT, last_seen_at TEXT,
  email_domain TEXT, segment TEXT
);
-- Names and contact details, only when INCLUDE_CONTACT_DETAILS is on (internal action lists).
-- Never sent to the Claude passes, which work from aggregate metrics only.
CREATE TABLE people (
  contact_id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT, email TEXT, job_title TEXT,
  company_text TEXT, state TEXT
);
CREATE TABLE owners (owner_id TEXT PRIMARY KEY, name TEXT, active INTEGER);
CREATE TABLE lifecycle_labels (stage TEXT PRIMARY KEY, label TEXT);
CREATE TABLE companies (
  company_id TEXT PRIMARY KEY, name TEXT, domain TEXT, industry TEXT, state TEXT, created_at TEXT,
  type TEXT, city TEXT, num_contacts INTEGER
);
CREATE TABLE deals (
  deal_id TEXT PRIMARY KEY, amount REAL, stage TEXT, pipeline TEXT,
  created_at TEXT, closed_at TEXT, closed_month TEXT,
  is_closed INTEGER, is_won INTEGER, contact_id TEXT, company_id TEXT,
  deal_group TEXT,  -- optional grouping, e.g. sponsorship tier or deal type
  deal_name TEXT    -- only with INCLUDE_CONTACT_DETAILS
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


def _form_label(v: str | None) -> str | None:
    """'Contact - INDATEL: #gform_4' -> 'Contact' (page/form name without site and form id)."""
    if not v or not str(v).strip():
        return None
    s = str(v).split(":")[0]
    for sep in (" - ", " | "):
        if sep in s:
            parts = [x.strip() for x in s.split(sep)]
            s = parts[1] if parts[0].isupper() and len(parts) > 1 else parts[0]
    return s.strip() or None


def _bool(v) -> bool | None:
    if v in (None, ""):
        return None
    return str(v).lower() == "true"


PERSONAL_DOMAINS = frozenset({"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com",
                              "msn.com", "live.com", "comcast.net", "me.com", "protonmail.com", "att.net"})


def _int(v) -> int:
    return int(_num(v) or 0)


def build_db(raw: dict[str, list[dict]], db_path: Path,
             exclude_contact_ids: frozenset[str] = frozenset(), include_details: bool = False,
             internal_domains: frozenset[str] = frozenset()) -> sqlite3.Connection:
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

    con.executemany("INSERT OR IGNORE INTO companies VALUES (?,?,?,?,?,?,?,?,?)", [
        (str(c["id"]), p.get("name"), _domain(p.get("domain")), p.get("industry"), p.get("state"),
         _dt(p.get("createdate")), (p.get("type") or "").strip() or None, p.get("city"),
         _int(p.get("num_associated_contacts")))
        for c in raw.get("companies", []) for p in [c.get("properties", {})]
    ])
    company_by_domain: dict[str, str] = {}
    for co_id, dom in con.execute("SELECT company_id, domain FROM companies WHERE domain IS NOT NULL ORDER BY company_id"):
        company_by_domain.setdefault(dom, co_id)

    seen: set[str] = set()
    contact_rows, people_rows = [], []
    internal_dropped = 0
    for c in raw.get("contacts", []):
        cid = str(c["id"])
        if cid in exclude_contact_ids or cid in seen:
            continue
        seen.add(cid)
        p = c.get("properties", {})
        email = (p.get("email") or "").strip().lower()
        email_domain = email.split("@")[1] if "@" in email else None
        if email_domain and email_domain in internal_domains:
            internal_dropped += 1  # staff records aren't marketing contacts
            continue
        ca = _dt(p.get("createdate"))
        # Engagement: explicit flags (aggregate-built rows), counts, or HubSpot's last open/click dates.
        open_n, click_n = _int(p.get("hs_email_open")), _int(p.get("hs_email_click"))
        opened = p.get("_email_opened", bool(p.get("hs_email_last_open_date")) or open_n > 0)
        clicked = p.get("_email_clicked", bool(p.get("hs_email_last_click_date")) or click_n > 0)
        forms = _int(p.get("num_conversion_events"))
        company_id = _first(c.get("associations"), "companies") or p.get("associatedcompanyid") or (
            company_by_domain.get(email_domain) if email_domain and email_domain not in PERSONAL_DOMAINS else None)
        contact_rows.append((cid, ca, _month(ca), p.get("lifecyclestage"), p.get("hs_analytics_source"),
                             resolve_campaign(p.get("hs_analytics_first_touch_converting_campaign")),
                             str(company_id) if company_id else None, p.get("email_hash"),
                             int(bool(opened)), int(bool(clicked)), int(forms > 0),
                             _form_label(p.get("first_conversion_event_name")), _int(p.get("hs_analytics_num_visits")),
                             _int(p.get("hs_email_delivered")) if p.get("hs_email_delivered") is not None
                             else int(bool(p.get("_email_delivered", opened))),
                             open_n, click_n, _dt(p.get("hs_email_last_open_date")), _dt(p.get("hs_email_last_click_date")),
                             int(str(p.get("hs_email_optout") or "").lower() == "true"), _int(p.get("num_notes")),
                             _dt(p.get("notes_last_contacted")), p.get("hubspot_owner_id"),
                             p.get("hs_analytics_source_data_1"), p.get("hs_analytics_source_data_2"),
                             _form_label(p.get("recent_conversion_event_name")), _dt(p.get("recent_conversion_date")),
                             _dt(p.get("hs_analytics_last_timestamp")), email_domain, None))
        if include_details:
            people_rows.append((cid, p.get("firstname"), p.get("lastname"), email or None, p.get("jobtitle"),
                                p.get("company"), p.get("state")))
    con.executemany(f"INSERT INTO contacts VALUES ({','.join('?' * 29)})", contact_rows)
    con.executemany("INSERT INTO people VALUES (?,?,?,?,?,?,?)", people_rows)
    con.execute("""UPDATE contacts SET segment = (SELECT type FROM companies co WHERE co.company_id = contacts.company_id)""")
    con.execute("CREATE TABLE IF NOT EXISTS load_notes (key TEXT PRIMARY KEY, value INTEGER)")
    con.execute("INSERT INTO load_notes VALUES ('internal_contacts_dropped', ?)", (internal_dropped,))
    con.executemany("INSERT OR IGNORE INTO owners VALUES (?,?,?)", [
        (str(o["id"]), o.get("name"), int(bool(o.get("active", True)))) for o in raw.get("owners", [])])
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
                     _first(d.get("associations"), "contacts"), _first(d.get("associations"), "companies"),
                     p.get("sponsorship_tier") or p.get("dealtype"),
                     p.get("dealname") if include_details else None))
    con.executemany("INSERT OR IGNORE INTO deals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
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
    con.executemany("INSERT INTO lifecycle_labels VALUES (?,?)", list((raw.get("lifecycle_labels") or {}).items()))
    con.executemany("INSERT INTO pipelines VALUES (?,?)", [
        (pl["id"], pl.get("label")) for pl in raw.get("pipelines", [])])
    con.commit()
    return con
