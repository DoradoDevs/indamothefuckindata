"""Salesforce extractor (read-only), for telecom service deals.

Auth: OAuth 2.0 client credentials flow on a Salesforce Connected App / External Client
App with a read-only "run as" integration user. Settings: SF_LOGIN_URL (the org's My
Domain URL), SF_CLIENT_ID, SF_CLIENT_SECRET, SF_API_VERSION.

Pulled (read-only SOQL):
  Opportunity            Id, AccountId, Amount, StageName, IsClosed, IsWon, CloseDate,
                         CreatedDate, Type, LeadSource
  OpportunityContactRole OpportunityId, ContactId, IsPrimary
  Account                Id, Website, Industry, BillingState, Type
  Contact                Id, AccountId, Email (hashed in memory), CreatedDate, LeadSource

Deliberately NOT pulled: opportunity/account/contact names, phone numbers, addresses,
descriptions, and any product, circuit, or service-location fields (CPNI-adjacent).
Not yet run against INDATEL's org; items marked VERIFY need a check on the first pull.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from pipeline.extract.hubspot import hash_email

log = logging.getLogger("pipeline.salesforce")

QUERIES = {
    "sf_opportunities": "SELECT Id, AccountId, Amount, StageName, IsClosed, IsWon, CloseDate, CreatedDate, "
                        "Type, LeadSource FROM Opportunity",
    "sf_opp_contacts": "SELECT OpportunityId, ContactId, IsPrimary FROM OpportunityContactRole",
    "sf_accounts": "SELECT Id, Website, Industry, BillingState, Type FROM Account",
    "sf_contacts": "SELECT Id, AccountId, Email, CreatedDate, LeadSource FROM Contact",
}

Fetch = Callable[[urllib.request.Request, float], bytes]


def _default_fetch(req: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class SalesforceError(RuntimeError):
    pass


class SalesforceClient:
    def __init__(self, login_url: str, client_id: str, client_secret: str, api_version: str = "v61.0",
                 fetch: Fetch | None = None, sleep: Callable[[float], None] = time.sleep, max_retries: int = 5):
        self.login_url = login_url.rstrip("/")
        self.client_id, self.client_secret = client_id, client_secret
        self.api_version = api_version
        self.fetch = fetch or _default_fetch
        self.sleep = sleep
        self.max_retries = max_retries
        self.token: str | None = None
        self.instance_url: str | None = None

    def authenticate(self) -> None:
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id,
                                       "client_secret": self.client_secret}).encode()
        req = urllib.request.Request(f"{self.login_url}/services/oauth2/token", data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            data = json.loads(self.fetch(req, 60))
        except urllib.error.HTTPError as e:
            raise SalesforceError(f"Salesforce login failed (HTTP {e.code}). Check SF_LOGIN_URL, SF_CLIENT_ID, "
                                  "SF_CLIENT_SECRET, and that the client credentials flow is enabled "
                                  "with a run-as user.") from e
        self.token, self.instance_url = data["access_token"], data["instance_url"].rstrip("/")

    def _get(self, path: str) -> dict:
        for attempt in range(self.max_retries):
            req = urllib.request.Request(self.instance_url + path, headers={
                "Authorization": f"Bearer {self.token}", "Accept": "application/json"})
            try:
                return json.loads(self.fetch(req, 120))
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self.authenticate()
                    continue
                if e.code in (500, 502, 503) and attempt < self.max_retries - 1:
                    self.sleep(min(2 ** attempt, 30))
                    continue
                raise
        raise SalesforceError(f"Gave up on {path}")

    def query_all(self, soql: str) -> list[dict]:
        path = f"/services/data/{self.api_version}/query?" + urllib.parse.urlencode({"q": soql})
        rows: list[dict] = []
        while path:
            page = self._get(path)
            for r in page.get("records", []):
                r.pop("attributes", None)
                rows.append(r)
            path = page.get("nextRecordsUrl") if not page.get("done", True) else None
        return rows


def pull(login_url: str, client_id: str, client_secret: str, hash_salt: str, api_version: str = "v61.0",
         fetch: Fetch | None = None, sleep: Callable[[float], None] = time.sleep) -> dict[str, list]:
    client = SalesforceClient(login_url, client_id, client_secret, api_version, fetch=fetch, sleep=sleep)
    client.authenticate()
    out: dict[str, list] = {}
    warnings: list[str] = []
    for name, soql in QUERIES.items():
        try:
            rows = client.query_all(soql)
        except urllib.error.HTTPError as e:
            if name == "sf_opportunities":
                raise SalesforceError(f"Can't read Opportunities (HTTP {e.code}). The integration user needs "
                                      "read access to Opportunity.") from e
            rows = []
            warnings.append(f"{name}: not available, HTTP {e.code} (check object/field permissions)")
        if name == "sf_contacts":
            for r in rows:
                r["EmailHash"] = hash_email(r.pop("Email", None), hash_salt)
        out[name] = rows
        log.info("pulled %d %s", len(rows), name)
    if not hash_salt:
        warnings.append("HASH_SALT not set: Salesforce contacts can't be matched to HubSpot by email")
    out["_sf_warnings"] = warnings
    return out
