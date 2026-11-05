"""HubSpot API extractor (private-app token).

Not yet run against a live portal — written against HubSpot's public v3 API docs and
needs a check once access is approved. Endpoints marked VERIFY depend on the HubSpot
tier/scopes we end up with.

Output shape matches extract/fixtures.py so the normalizer is shared:
  objects: {"id", "properties": {...}, "associations": {"companies": [ids], ...}}
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

BASE = "https://api.hubapi.com"

# Only the properties we analyze. Personal fields (name, email, phone, address) are
# deliberately NOT requested, so PII never lands in the raw pull from the real API.
PROPERTIES = {
    "contacts": ["createdate", "lastmodifieddate", "lifecyclestage", "hs_analytics_source",
                 "hs_analytics_first_touch_converting_campaign"],
    "companies": ["name", "industry", "state", "createdate"],
    # hs_is_closed_won / hs_is_closed are used instead of stage IDs: pipelines migrated
    # from Salesforce use GUID stage IDs, not "closedwon". Deal names aren't pulled;
    # they often contain customer names and aren't needed for analysis.
    "deals": ["amount", "amount_in_home_currency", "dealstage", "pipeline", "createdate", "closedate",
              "hs_is_closed_won", "hs_is_closed"],
}
ASSOCIATIONS = {"contacts": ["companies"], "companies": [], "deals": ["contacts", "companies"]}


class HubSpotClient:
    def __init__(self, token: str, max_retries: int = 5):
        self.token = token
        self.max_retries = max_retries

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        url = BASE + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(self.max_retries):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.max_retries - 1:
                    time.sleep(int(e.headers.get("Retry-After", 2 ** attempt)))
                    continue
                raise
        raise RuntimeError("unreachable")

    def list_objects(self, object_type: str) -> Iterator[dict]:
        """Full pull via the paginated list endpoint."""
        params = {"limit": 100, "properties": PROPERTIES[object_type], "archived": "false"}
        if ASSOCIATIONS[object_type]:
            params["associations"] = ASSOCIATIONS[object_type]
        after = None
        while True:
            if after:
                params["after"] = after
            page = self._request("GET", f"/crm/v3/objects/{object_type}", params)
            for obj in page.get("results", []):
                yield _flatten(obj)
            after = page.get("paging", {}).get("next", {}).get("after")
            if not after:
                return

    def campaigns(self) -> Iterator[dict]:
        """VERIFY: Marketing campaigns API v3; requires Marketing Hub + campaigns scope."""
        after = None
        while True:
            params = {"limit": 100, "properties": ["hs_name", "hs_start_date", "hs_end_date"]}
            if after:
                params["after"] = after
            page = self._request("GET", "/marketing/v3/campaigns", params)
            for c in page.get("results", []):
                yield {"id": c["id"], "properties": c.get("properties", {})}
            after = page.get("paging", {}).get("next", {}).get("after")
            if not after:
                return

    def email_stats(self) -> Iterator[dict]:
        """VERIFY: Marketing emails v3 with includeStats; field names need checking."""
        after = None
        while True:
            params = {"limit": 100, "includeStats": "true"}
            if after:
                params["after"] = after
            page = self._request("GET", "/marketing/v3/emails", params)
            for em in page.get("results", []):
                counters = (em.get("stats") or {}).get("counters", {})
                yield {
                    "email_id": em["id"],
                    "campaign_id": em.get("campaign"),
                    "send_date": (em.get("publishDate") or "")[:10] or None,
                    "sent": counters.get("sent", 0), "delivered": counters.get("delivered", 0),
                    "opened": counters.get("open", 0), "clicked": counters.get("click", 0),
                }
            after = page.get("paging", {}).get("next", {}).get("after")
            if not after:
                return


    def deal_pipelines(self) -> list[dict]:
        page = self._request("GET", "/crm/v3/pipelines/deals")
        return [{"id": pl["id"], "label": pl.get("label"),
                 "stages": [{"id": st["id"], "label": st.get("label"),
                             "probability": (st.get("metadata") or {}).get("probability")}
                            for st in pl.get("stages", [])]}
                for pl in page.get("results", [])]


def _flatten(obj: dict) -> dict:
    assoc = {k: [r["id"] for r in v.get("results", [])] for k, v in (obj.get("associations") or {}).items()}
    return {"id": obj["id"], "properties": obj.get("properties", {}), "associations": assoc}


def pull(token: str) -> dict[str, list[dict]]:
    client = HubSpotClient(token)
    out: dict[str, list[dict]] = {t: list(client.list_objects(t)) for t in PROPERTIES}
    for name, fn in (("pipelines", client.deal_pipelines), ("campaigns", client.campaigns),
                     ("email_stats", client.email_stats)):
        try:
            out[name] = list(fn())
        except urllib.error.HTTPError as e:
            # Missing scope/tier: keep going with CRM data, record the gap.
            out[name] = []
            out.setdefault("_warnings", []).append(f"{name}: HTTP {e.code}")
    # TODO: website traffic. HubSpot's traffic analytics endpoint is legacy and
    # tier-dependent; decide on source (HubSpot vs. GA4) before wiring it.
    out["web_traffic"] = []
    return out
