"""Import HubSpot UI exports (CSV) into the same raw shape as the API extractor.

For when someone with HubSpot export permission can provide files before the API
token exists. Point it at a folder: `python -m pipeline.run --source csv --csv-dir DIR`.

* File type (contacts / companies / deals) is detected from the columns, not the name.
* Column names are matched case-insensitively against known HubSpot export labels.
* Display values ("Organic Search", "Marketing Qualified Lead") become API values.
* Personal columns (names, phone, address) are never read. Email, if present, is turned
  into a keyed one-way hash (HASH_SALT) and the raw value is discarded.
* Exports should be CSV. If someone sends .xlsx, open it and "Save as CSV".
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

from pipeline.extract.hubspot import hash_email

log = logging.getLogger("pipeline.csv")

ALIASES = {
    "id": ["record id", "contact id", "company id", "deal id", "hs_object_id"],
    "createdate": ["create date", "created date", "createdate", "date created"],
    "lifecyclestage": ["lifecycle stage", "lifecyclestage"],
    "source": ["original source", "original source type", "hs_analytics_source"],
    "campaign": ["first touch converting campaign", "hs_analytics_first_touch_converting_campaign"],
    "email": ["email", "email address"],
    "company_ids": ["associated company ids (primary)", "associated company ids", "associated company id",
                    "company id (primary)"],
    "contact_ids": ["associated contact ids", "associated contact id"],
    "name": ["company name", "name"],
    "domain": ["company domain name", "website url", "domain"],
    "industry": ["industry"],
    "state": ["state/region", "state", "state/region code"],
    "dealstage": ["deal stage", "dealstage"],
    "pipeline": ["pipeline"],
    "amount": ["amount in company currency", "amount in home currency", "amount"],
    "closedate": ["close date", "closedate"],
    "is_won": ["is closed won", "is_closed_won", "hs_is_closed_won"],
    "is_closed": ["is closed", "is_closed", "hs_is_closed"],
}

SOURCE_LABELS = {
    "organic search": "ORGANIC_SEARCH", "paid search": "PAID_SEARCH", "email marketing": "EMAIL_MARKETING",
    "organic social": "SOCIAL_MEDIA", "social media": "SOCIAL_MEDIA", "paid social": "PAID_SOCIAL",
    "referrals": "REFERRALS", "direct traffic": "DIRECT_TRAFFIC", "offline sources": "OFFLINE",
    "offline": "OFFLINE", "other campaigns": "OTHER_CAMPAIGNS", "ai referrals": "AI_REFERRALS",
}


def _key(header: str) -> str:
    return re.sub(r"\s+", " ", header.strip().lower())


def _column_map(headers: list[str]) -> dict[str, str]:
    present = {_key(h): h for h in headers}
    out = {}
    for field, names in ALIASES.items():
        for n in names:
            if n in present:
                out[field] = present[n]
                break
    return out


def _kind(cols: dict[str, str]) -> str | None:
    if "dealstage" in cols:
        return "deals"
    if "lifecyclestage" in cols or "email" in cols or "source" in cols:
        return "contacts"
    if "industry" in cols or "domain" in cols:
        return "companies"
    return None


def _date(v: str | None) -> str | None:
    """HubSpot exports use '2026-01-05 14:03' or '2026-01-05'; also accept ISO."""
    if not v or not v.strip():
        return None
    v = v.strip().replace(" ", "T", 1)
    return v if "T" in v else v + "T00:00:00"


def _ids(v: str | None) -> list[str]:
    return [x.strip() for x in re.split(r"[;,]", v or "") if x.strip()]


def _source(v: str | None) -> str | None:
    if not v or not v.strip():
        return None
    return SOURCE_LABELS.get(v.strip().lower(), re.sub(r"\W+", "_", v.strip()).upper())


def _stage(v: str | None) -> str | None:
    return re.sub(r"[\s_-]+", "", v.strip().lower()) if v and v.strip() else None


def _bool(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    return "true" if v.strip().lower() in ("true", "yes", "1") else "false"


def load_dir(folder: Path, hash_salt: str = "") -> dict[str, list]:
    out: dict[str, list] = {"contacts": [], "companies": [], "deals": [], "pipelines": [], "campaigns": [],
                            "email_stats": [], "web_traffic": [], "_warnings": []}
    files = sorted(p for p in Path(folder).iterdir() if p.suffix.lower() == ".csv")
    if not files:
        raise FileNotFoundError(f"No .csv files in {folder}")
    pipelines: dict[str, None] = {}
    for path in files:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            cols = _column_map(reader.fieldnames or [])
            kind = _kind(cols)
            if kind is None or "id" not in cols:
                out["_warnings"].append(f"{path.name}: couldn't tell what this file is (no Record ID or known "
                                        "columns); skipped")
                continue
            g = lambda row, f: row.get(cols[f]) if f in cols else None  # noqa: E731
            for row in reader:
                rid = (g(row, "id") or "").strip()
                if not rid:
                    continue
                if kind == "contacts":
                    out["contacts"].append({"id": rid, "properties": {
                        "createdate": _date(g(row, "createdate")),
                        "lifecyclestage": _stage(g(row, "lifecyclestage")),
                        "hs_analytics_source": _source(g(row, "source")),
                        "hs_analytics_first_touch_converting_campaign": (g(row, "campaign") or "").strip() or None,
                        "email_hash": hash_email(g(row, "email"), hash_salt),
                    }, "associations": {"companies": _ids(g(row, "company_ids"))}})
                elif kind == "companies":
                    out["companies"].append({"id": rid, "properties": {
                        "name": g(row, "name"), "domain": g(row, "domain"), "industry": g(row, "industry") or None,
                        "state": g(row, "state") or None, "createdate": _date(g(row, "createdate"))}})
                else:
                    stage_label = (g(row, "dealstage") or "").strip()
                    is_won = _bool(g(row, "is_won"))
                    is_closed = _bool(g(row, "is_closed"))
                    if is_won is None:
                        is_won = "true" if "closed won" in stage_label.lower() else "false"
                    if is_closed is None:
                        is_closed = "true" if stage_label.lower().startswith("closed") else "false"
                    pl = (g(row, "pipeline") or "").strip() or None
                    if pl:
                        pipelines[pl] = None
                    out["deals"].append({"id": rid, "properties": {
                        "amount": (g(row, "amount") or "").replace(",", "").replace("$", "").strip() or None,
                        "dealstage": _stage(stage_label), "pipeline": pl,
                        "createdate": _date(g(row, "createdate")), "closedate": _date(g(row, "closedate")),
                        "hs_is_closed_won": is_won, "hs_is_closed": is_closed,
                    }, "associations": {"contacts": _ids(g(row, "contact_ids")),
                                        "companies": _ids(g(row, "company_ids"))}})
            log.info("%s: read as %s", path.name, kind)
    out["pipelines"] = [{"id": p, "label": p, "stages": []} for p in pipelines]
    if not hash_salt and any(c["properties"].get("email_hash") is None for c in out["contacts"]):
        out["_warnings"].append("HASH_SALT not set: emails were discarded without hashing")
    out["_warnings"].append("Loaded from CSV export: campaigns, email stats, and traffic aren't included")
    return out
