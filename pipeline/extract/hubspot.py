"""HubSpot API extractor (read-only private-app token).

Written against HubSpot's public v3 API and the live account's structure (checked via
the connector). Not yet run with a token. Items marked VERIFY need a look on the
first real pull; the pull validation report (pipeline/validate.py) will flag them.

Output shape matches extract/fixtures.py so the normalizer is shared:
  objects: {"id", "properties": {...}, "associations": {"companies": [ids], ...}}

Personal data:
  * Names, phone numbers, addresses, and deal names are never requested.
  * Email is requested only to compute a keyed one-way hash (HMAC-SHA256 with
    HASH_SALT) for matching contacts to Salesforce. The raw email is discarded in
    memory and never written anywhere.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator

BASE = "https://api.hubapi.com"
log = logging.getLogger("pipeline.hubspot")

PROPERTIES = {
    "contacts": ["createdate", "lastmodifieddate", "lifecyclestage", "hs_analytics_source",
                 "hs_analytics_first_touch_converting_campaign", "email",
                 "hs_email_last_open_date", "hs_email_last_click_date", "num_conversion_events",
                 "first_conversion_event_name", "hs_analytics_num_visits"],
    "companies": ["name", "domain", "industry", "state", "createdate"],
    # hs_is_closed_won / hs_is_closed are used instead of stage IDs: pipelines migrated
    # from Salesforce use GUID stage IDs, not "closedwon". Deal names aren't pulled;
    # they often contain customer names and aren't needed for analysis.
    "deals": ["amount", "amount_in_home_currency", "dealstage", "pipeline", "createdate", "closedate",
              "hs_is_closed_won", "hs_is_closed", "dealtype"],
}
ASSOCIATIONS = {"contacts": ["companies"], "companies": [], "deals": ["contacts", "companies"]}
SCOPES_HINT = {
    "contacts": "crm.objects.contacts.read", "companies": "crm.objects.companies.read",
    "deals": "crm.objects.deals.read", "pipelines": "crm.objects.deals.read",
    "campaigns": "marketing.campaigns.read", "email_stats": "content",
}

Fetch = Callable[[urllib.request.Request, float], bytes]


def _default_fetch(req: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class HubSpotError(RuntimeError):
    pass


def hash_email(email: str | None, salt: str) -> str | None:
    """Keyed one-way hash of a normalized email. Same salt must be used for Salesforce."""
    if not email or not salt:
        return None
    norm = email.strip().lower()
    if "@" not in norm:
        return None
    return hmac.new(salt.encode(), norm.encode(), hashlib.sha256).hexdigest()


class HubSpotClient:
    def __init__(self, token: str, max_retries: int = 6, fetch: Fetch | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.token = token
        self.max_retries = max_retries
        self.fetch = fetch or _default_fetch
        self.sleep = sleep
        self.requests = 0

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        url = BASE + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        for attempt in range(self.max_retries):
            req = urllib.request.Request(url, method=method, headers={
                "Authorization": f"Bearer {self.token}", "Accept": "application/json"})
            try:
                self.requests += 1
                return json.loads(self.fetch(req, 60))
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise HubSpotError("HubSpot rejected the token (401). Check HUBSPOT_TOKEN.") from e
                retryable = e.code in (429, 500, 502, 503, 504)
                if retryable and attempt < self.max_retries - 1:
                    wait = float((e.headers or {}).get("Retry-After") or min(2 ** attempt, 30))
                    log.warning("HubSpot %s on %s; retrying in %.0fs", e.code, path, wait)
                    self.sleep(wait)
                    continue
                raise
            except urllib.error.URLError as e:
                if attempt < self.max_retries - 1:
                    self.sleep(min(2 ** attempt, 30))
                    continue
                raise HubSpotError(f"Network error reaching HubSpot: {e.reason}") from e
        raise HubSpotError(f"Gave up on {path} after {self.max_retries} attempts")

    def _paginate(self, path: str, params: dict) -> Iterator[dict]:
        params = dict(params)
        while True:
            page = self._request("GET", path, params)
            yield from page.get("results", [])
            after = (page.get("paging") or {}).get("next", {}).get("after")
            if not after:
                return
            params["after"] = after

    def list_objects(self, object_type: str, warnings: list[str], salt: str = "") -> Iterator[dict]:
        params = {"limit": 100, "properties": PROPERTIES[object_type], "archived": "false"}
        if ASSOCIATIONS[object_type]:
            params["associations"] = ASSOCIATIONS[object_type]
        truncated = 0
        for obj in self._paginate(f"/crm/v3/objects/{object_type}", params):
            flat, was_truncated = _flatten(obj)
            truncated += was_truncated
            props = flat["properties"]
            if "email" in props:
                props["email_hash"] = hash_email(props.pop("email"), salt)
            yield flat
        if truncated:
            warnings.append(f"{object_type}: {truncated} records had more associations than one page returns; "
                            f"only the first page was used (VERIFY)")

    def deal_pipelines(self) -> list[dict]:
        page = self._request("GET", "/crm/v3/pipelines/deals")
        return [{"id": pl["id"], "label": pl.get("label"),
                 "stages": [{"id": st["id"], "label": st.get("label"),
                             "probability": (st.get("metadata") or {}).get("probability")}
                            for st in pl.get("stages", [])]}
                for pl in page.get("results", [])]

    def lifecycle_labels(self) -> dict[str, str]:
        """Display labels for lifecycle stages (accounts often rename or add stages)."""
        prop = self._request("GET", "/crm/v3/properties/contacts/lifecyclestage")
        return {o["value"]: o.get("label", o["value"]) for o in prop.get("options", [])}

    def campaigns(self) -> Iterator[dict]:
        """VERIFY: Marketing campaigns API v3; needs Marketing Hub + campaigns read scope."""
        for c in self._paginate("/marketing/v3/campaigns",
                                {"limit": 100, "properties": ["hs_name", "hs_start_date", "hs_end_date"]}):
            yield {"id": c["id"], "properties": c.get("properties", {})}

    def email_stats(self) -> Iterator[dict]:
        """VERIFY: Marketing emails v3 with includeStats; counter names need checking."""
        for em in self._paginate("/marketing/v3/emails", {"limit": 100, "includeStats": "true"}):
            counters = (em.get("stats") or {}).get("counters", {})
            yield {
                "email_id": em["id"],
                "campaign_id": em.get("campaign"),
                "send_date": (em.get("publishDate") or "")[:10] or None,
                "sent": counters.get("sent", 0), "delivered": counters.get("delivered", 0),
                "opened": counters.get("open", 0), "clicked": counters.get("click", 0),
            }


def _flatten(obj: dict) -> tuple[dict, bool]:
    """Association lists -> ids, primary association first, duplicates removed."""
    assoc: dict[str, list[str]] = {}
    truncated = False
    for kind, block in (obj.get("associations") or {}).items():
        results = block.get("results", [])
        if (block.get("paging") or {}).get("next"):
            truncated = True
        primary = [r["id"] for r in results if "unlabeled" not in str(r.get("type", ""))]
        rest = [r["id"] for r in results]
        seen: list[str] = []
        for i in primary + rest:
            if str(i) not in seen:
                seen.append(str(i))
        assoc[kind] = seen
    return {"id": str(obj["id"]), "properties": obj.get("properties", {}), "associations": assoc}, truncated


def pull(token: str, hash_salt: str = "", fetch: Fetch | None = None,
         sleep: Callable[[float], None] = time.sleep) -> dict[str, list]:
    client = HubSpotClient(token, fetch=fetch, sleep=sleep)
    warnings: list[str] = []
    if not hash_salt:
        warnings.append("HASH_SALT not set: contact emails were discarded without hashing, so "
                        "Salesforce matching by contact won't be possible for this run")
    out: dict[str, list] = {}
    for object_type in PROPERTIES:
        try:
            out[object_type] = list(client.list_objects(object_type, warnings, hash_salt))
            log.info("pulled %d %s", len(out[object_type]), object_type)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                raise HubSpotError(f"Token lacks access to {object_type} (403). Ask the HubSpot admin to add "
                                   f"the '{SCOPES_HINT[object_type]}' scope to the private app.") from e
            raise
    optional = (("pipelines", client.deal_pipelines), ("campaigns", client.campaigns),
                ("email_stats", client.email_stats))
    for name, fn in optional:
        try:
            out[name] = list(fn())
        except urllib.error.HTTPError as e:
            out[name] = []
            hint = f" (needs '{SCOPES_HINT[name]}' scope or a higher HubSpot tier)" if e.code in (403, 404) else ""
            warnings.append(f"{name}: not available, HTTP {e.code}{hint}")
    try:
        out["lifecycle_labels"] = client.lifecycle_labels()
    except urllib.error.HTTPError as e:
        warnings.append(f"lifecycle stage labels: not available, HTTP {e.code}")
    # Website traffic: HubSpot's traffic analytics API is legacy and tier-dependent.
    # Decide on the source (HubSpot vs. Google Analytics) before wiring it.
    out["web_traffic"] = []
    out["_warnings"] = warnings
    out["_meta"] = [{"requests": client.requests}]
    return out
