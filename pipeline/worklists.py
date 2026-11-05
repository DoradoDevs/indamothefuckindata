"""Action lists: named people and companies to act on, built from the warehouse.

Deterministic SQL only (no Claude). Requires INCLUDE_CONTACT_DETAILS so the warehouse
has names and emails; otherwise lists are produced with counts only. Internal use:
the lists contain personal contact details and sponsorship values.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime

from pipeline.transform.normalize import PERSONAL_DOMAINS

ROLE_MAILBOX = re.compile(r"^(info|marketing|sales|support|admin|billing|noreply|no-reply|office|contact|hello|team|"
                          r"noc|outage|alerts?|notifications?)[@.]", re.I)


def _days_since(iso: str | None, now: datetime) -> int | None:
    if not iso:
        return None
    try:
        return (now - datetime.fromisoformat(iso)).days
    except ValueError:
        return None


def _date(iso: str | None) -> str | None:
    return iso[:10] if iso else None


def build(con: sqlite3.Connection, converted: frozenset[str], portal_id: str = "",
          key_segments: tuple[str, ...] = ("Member", "Customer"), now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    has_people = con.execute("SELECT COUNT(*) FROM people").fetchone()[0] > 0
    labels = dict(con.execute("SELECT stage, label FROM lifecycle_labels"))
    owners = dict(con.execute("SELECT owner_id, name FROM owners"))
    ph = ",".join("?" * len(converted)) or "''"
    conv = f"COALESCE(c.lifecycle_stage,'') IN ({ph})"
    args = tuple(converted)

    def link(kind: str, rid: str) -> str | None:
        return f"https://app.hubspot.com/contacts/{portal_id}/record/{kind}/{rid}" if portal_id and rid else None

    base = """SELECT c.contact_id, TRIM(COALESCE(p.first_name,'') || ' ' || COALESCE(p.last_name,'')) AS name,
                     p.email, COALESCE(co.name, p.company_text) AS company, COALESCE(c.segment, '') AS segment,
                     p.job_title, c.lifecycle_stage, c.source, c.source_detail_1, c.owner_id, c.email_domain,
                     c.recent_form, c.recent_form_at, c.web_visits, c.last_seen_at, c.email_open_count,
                     c.email_click_count, c.last_open, c.last_click, c.last_contacted, c.sales_activities,
                     c.created_at, c.email_delivered, c.opted_out
              FROM contacts c LEFT JOIN people p ON p.contact_id = c.contact_id
              LEFT JOIN companies co ON co.company_id = c.company_id"""
    cols = ["id", "name", "email", "company", "segment", "title", "stage", "source", "source_detail", "owner",
            "domain", "form", "form_at", "visits", "last_seen", "opens", "clicks", "last_open", "last_click",
            "last_contacted", "activities", "created", "delivered", "opted_out"]

    def rows(where: str, order: str, a: tuple = ()) -> list[dict]:
        out = []
        for r in con.execute(f"{base} WHERE {where} ORDER BY {order}", a):
            d = dict(zip(cols, r))
            d["stage"] = labels.get(d["stage"], d["stage"])
            d["owner"] = owners.get(d["owner"], d["owner"])
            d["link"] = link("0-1", d["id"])
            for k in ("form_at", "last_seen", "last_open", "last_click", "last_contacted", "created"):
                d[k] = _date(d[k])
            out.append(d)
        return out

    lists = []

    # 1. Website inquiries with no logged follow-up
    inq = rows(f"c.form_submitted = 1 AND c.sales_activities = 0 AND NOT ({conv})", "c.recent_form_at DESC", args)
    for d in inq:
        d["days_waiting"] = _days_since(d["form_at"] and d["form_at"] + "T00:00:00+00:00", now)
        d["personal_email"] = "yes" if d["domain"] in PERSONAL_DOMAINS else ""
    lists.append({
        "id": "inquiries", "title": "Website inquiries with no logged follow-up",
        "why": "These people filled in a form on the website and HubSpot shows no call, email, meeting, or note since. "
               "Newest first. If follow-up happens in Outlook or Salesforce, log it in HubSpot so this list stays accurate.",
        "columns": [["form_at", "Submitted"], ["days_waiting", "Days waiting"], ["name", "Name"], ["company", "Company"],
                    ["email", "Email"], ["form", "Form"], ["source", "Came from"], ["visits", "Site visits"],
                    ["personal_email", "Personal email"], ["owner", "Owner"]],
        "rows": inq})

    # 2. Warm prospects: clicked marketing email, not yet converted
    warm = rows(f"c.email_clicked = 1 AND NOT ({conv})", "c.last_click DESC, c.email_click_count DESC", args)
    lists.append({
        "id": "warm", "title": "Warm prospects (clicked a marketing email)",
        "why": "Prospects who clicked a link in a marketing email: the clearest sign of interest HubSpot records. "
               "Most recent click first.",
        "columns": [["last_click", "Last click"], ["name", "Name"], ["company", "Company"], ["segment", "Company type"],
                    ["title", "Title"], ["clicks", "Emails clicked"], ["opens", "Emails opened"], ["email", "Email"],
                    ["owner", "Owner"]],
        "rows": warm})

    # 3. Sponsor renewals: won deals grouped by company
    spons = []
    for co_id, company, segment, tiers, packages, value, n, contact_id in con.execute("""
            SELECT co.company_id, co.name, co.type, GROUP_CONCAT(DISTINCT d.deal_group), GROUP_CONCAT(d.deal_name, '; '),
                   SUM(d.amount), COUNT(*), MIN(d.contact_id)
            FROM deals d JOIN companies co ON co.company_id = d.company_id WHERE d.is_won = 1
            GROUP BY co.company_id ORDER BY SUM(d.amount) DESC"""):
        person = rows("c.contact_id = ?", "c.contact_id", (contact_id,)) if contact_id else []
        pr = person[0] if person else {}
        spons.append({"company": company, "segment": segment or "", "tiers": tiers, "packages": packages,
                      "value": value, "deals": n, "name": pr.get("name"), "email": pr.get("email"),
                      "last_open": pr.get("last_open"), "last_contacted": pr.get("last_contacted"),
                      "link": link("0-2", co_id)})
    lists.append({
        "id": "renewals", "title": "2026 sponsors to renew",
        "why": "Every company that won a sponsorship, largest first, with the contact on the deal. Start renewal "
               "conversations with the top tiers well before the next season's pipeline opens.",
        "columns": [["company", "Company"], ["segment", "Company type"], ["tiers", "Tier"], ["packages", "Packages"],
                    ["value", "Value"], ["name", "Contact"], ["email", "Email"], ["last_open", "Last opened email"],
                    ["last_contacted", "Last contacted"]],
        "rows": spons})

    # 4. Lapsed: lifecycle stage labelled Inactive
    inactive = [s for s, lab in labels.items() if (lab or "").lower() == "inactive"]
    in_ph = ",".join("?" * len(inactive))
    lapsed = rows(f"c.lifecycle_stage IN ({in_ph})", "c.last_open DESC", tuple(inactive)) if inactive else []
    lists.append({
        "id": "lapsed", "title": "Lapsed contacts (Inactive)",
        "why": "Contacts who reached Active and are now Inactive. Candidates for a check-in or win-back.",
        "columns": [["name", "Name"], ["company", "Company"], ["segment", "Company type"], ["title", "Title"],
                    ["last_open", "Last opened email"], ["last_contacted", "Last contacted"], ["email", "Email"],
                    ["owner", "Owner"]],
        "rows": lapsed})

    # 5. Key accounts never sent marketing email
    ks = ",".join("?" * len(key_segments)) or "''"
    gap = rows(f"c.segment IN ({ks}) AND c.email_delivered = 0 AND c.opted_out = 0 AND p.email IS NOT NULL",
               "c.segment, company", tuple(key_segments)) if has_people else []
    lists.append({
        "id": "not_emailed", "title": f"{' / '.join(key_segments)} contacts never sent marketing email",
        "why": "People at key account types who have never received a marketing email from HubSpot. Check whether "
               "they should be on the newsletter and event lists.",
        "columns": [["segment", "Company type"], ["company", "Company"], ["name", "Name"], ["title", "Title"],
                    ["email", "Email"], ["owner", "Owner"]],
        "rows": gap})

    # 6. Data cleanup
    clean = []
    for d in rows("1=1", "c.created_at") if has_people else []:
        issues = []
        nm = (d["name"] or "").lower()
        if re.search(r"\btest\b|\(sample contact\)", nm):
            issues.append("Test or sample record")
        if d["email"] and ROLE_MAILBOX.search(d["email"]):
            issues.append("Shared or system mailbox")
        if not d["email"]:
            issues.append("No email address")
        if d["opted_out"] and d["stage"] and d["stage"].lower() == "active":
            issues.append("Active but unsubscribed from marketing email")
        if issues:
            d["issue"] = "; ".join(issues)
            clean.append(d)
    lists.append({
        "id": "cleanup", "title": "Records to clean up",
        "why": "Test records, shared mailboxes, and records missing an email. Fixing these makes every other number "
               "more accurate.",
        "columns": [["issue", "Issue"], ["name", "Name"], ["email", "Email"], ["company", "Company"],
                    ["stage", "Stage"], ["created", "Added"]],
        "rows": clean})

    for lst in lists:
        lst["count"] = len(lst["rows"])
        if not has_people:
            lst["rows"] = []
            lst["note"] = "Names aren't included in this run (INCLUDE_CONTACT_DETAILS is off)."
    return {"generated_at": now.isoformat(), "has_details": has_people, "lists": lists}
