"""Salesforce extractor (fake API) and HubSpot <-> Salesforce matching rules."""
import io
import json
import urllib.error
import urllib.parse

import pytest

from pipeline.extract import salesforce
from pipeline.extract.hubspot import hash_email
from pipeline.metrics.service import compute_service
from pipeline.transform.normalize import build_db
from pipeline.transform.salesforce import build_sf


class FakeSF:
    def __init__(self, tables, fail_login=False):
        self.tables, self.fail_login, self.soql = tables, fail_login, []

    def __call__(self, req, timeout):
        url = urllib.parse.urlparse(req.full_url)
        if url.path == "/services/oauth2/token":
            if self.fail_login:
                raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b"{}"))
            body = urllib.parse.parse_qs(req.data.decode())
            assert body["grant_type"] == ["client_credentials"]
            return json.dumps({"access_token": "at", "instance_url": "https://org.my.salesforce.com"}).encode()
        assert req.get_header("Authorization") == "Bearer at"
        if url.path.endswith("/query/next"):
            return json.dumps({"done": True, "records": [{"attributes": {}, "Id": "006B", "IsWon": False}]}).encode()
        soql = urllib.parse.parse_qs(url.query)["q"][0]
        self.soql.append(soql)
        obj = soql.split(" FROM ")[1]
        if obj == "Opportunity":  # two pages
            return json.dumps({"done": False, "nextRecordsUrl": "/services/data/v61.0/query/next",
                               "records": [{"attributes": {}, "Id": "006A", "IsWon": True}]}).encode()
        return json.dumps({"done": True, "records": self.tables.get(obj, [])}).encode()


def test_pull_paginates_hashes_and_never_requests_names():
    fake = FakeSF({"Contact": [{"attributes": {}, "Id": "003A", "Email": "Pat@Example.com"}]})
    out = salesforce.pull("https://org.my.salesforce.com", "id", "secret", "salt", fetch=fake, sleep=lambda s: None)
    assert [o["Id"] for o in out["sf_opportunities"]] == ["006A", "006B"]
    c = out["sf_contacts"][0]
    assert "Email" not in c and c["EmailHash"] == hash_email("pat@example.com", "salt")
    for soql in fake.soql:
        fields = {f.strip() for f in soql.removeprefix("SELECT ").split(" FROM ")[0].split(",")}
        assert not fields & {"Name", "FirstName", "LastName", "Phone", "MobilePhone", "Description",
                             "BillingStreet", "MailingStreet"}


def test_login_failure_message():
    with pytest.raises(salesforce.SalesforceError, match="SF_CLIENT_ID"):
        salesforce.pull("https://x", "id", "s", "salt", fetch=FakeSF({}, fail_login=True))


def _hs_raw():
    h = lambda e: hash_email(e, "salt")  # noqa: E731
    return {
        "contacts": [
            {"id": "1", "properties": {"createdate": "2026-01-01T00:00:00Z", "email_hash": h("a@x.com"),
                                       "hs_analytics_first_touch_converting_campaign": "web"},
             "associations": {"companies": ["10"]}},
            {"id": "2", "properties": {"createdate": "2026-06-01T00:00:00Z", "email_hash": h("b@y.com")},
             "associations": {"companies": ["20"]}},
        ],
        "companies": [{"id": "10", "properties": {"domain": "x.com"}}, {"id": "20", "properties": {"domain": "y.com"}}],
        "campaigns": [{"id": "web", "properties": {"hs_name": "Webinar"}}],
    }


def test_matching_and_credit_rules(tmp_path):
    h = lambda e: hash_email(e, "salt")  # noqa: E731
    sf = {
        "sf_accounts": [{"Id": "A1", "Website": "https://www.x.com"}, {"Id": "A2", "Website": "y.com"},
                        {"Id": "A3", "Website": "nobody.org"}],
        "sf_contacts": [{"Id": "C1", "AccountId": "A1", "EmailHash": h("a@x.com")}],
        "sf_opp_contacts": [{"OpportunityId": "O1", "ContactId": "C1", "IsPrimary": True}],
        "sf_opportunities": [
            # matched by contact email; marketing came first -> credited
            {"Id": "O1", "AccountId": "A1", "Amount": 1000, "IsClosed": True, "IsWon": True,
             "CreatedDate": "2026-02-01T00:00:00.000+0000", "CloseDate": "2026-03-15"},
            # matched by domain only; deal started BEFORE the HubSpot contact -> not credited
            {"Id": "O2", "AccountId": "A2", "Amount": 500, "IsClosed": True, "IsWon": True,
             "CreatedDate": "2026-03-01T00:00:00.000+0000", "CloseDate": "2026-04-01"},
            # Salesforce-only customer -> unmatched
            {"Id": "O3", "AccountId": "A3", "Amount": 700, "IsClosed": True, "IsWon": False,
             "CreatedDate": "2026-03-01T00:00:00.000+0000", "CloseDate": "2026-04-01"},
        ],
    }
    raw = _hs_raw() | sf
    con = build_db(raw, tmp_path / "w.sqlite")
    stats = build_sf(con, raw)
    assert stats == {"contact": 1, "account": 1, "none": 1}
    rows = {r[0]: r[1:] for r in con.execute("SELECT opp_id, match_method, hs_contact_id, credited FROM service_deals")}
    assert rows["O1"] == ("contact", "1", 1)
    assert rows["O2"] == ("account", "2", 0)
    assert rows["O3"] == ("none", None, 0)

    m = {x.id: x for x in compute_service(con)}
    assert m["service.overview.deals_won"].value == 2
    assert m["service.overview.won_credited_rate"].value == 0.5
    assert m["service.campaign.web.won_value"].value == 1000.0       # only the credited deal
    assert m["service.campaign.none.won_value"].value == 0.0          # O2 not credited
    assert m["quality.service_deals_unmatched_rate"].value == pytest.approx(1 / 3, abs=1e-4)


def test_no_salesforce_means_no_service_metrics(tmp_path):
    con = build_db(_hs_raw(), tmp_path / "w.sqlite")
    assert compute_service(con) == []


def test_fixture_service_pattern_is_detected(tmp_path):
    from pipeline.extract.fixtures import generate, generate_salesforce
    raw = generate()
    raw.update(generate_salesforce(raw))
    con = build_db(raw, tmp_path / "w.sqlite")
    stats = build_sf(con, raw)
    assert stats["none"] >= 25
    m = {x.id: x for x in compute_service(con)}
    t = m["test.service_campaign.cmp-001.contact_to_service_win_rate"]
    assert t.significant and t.value > 0
    assert 0.8 < m["service.overview.won_credited_rate"].value < 1.0
