"""HubSpot extractor against a fake HubSpot API: pagination, retries, scopes, PII."""
import io
import json
import urllib.error
import urllib.parse

import pytest

from pipeline.extract import hubspot


def _http_error(url, code, headers=None):
    return urllib.error.HTTPError(url, code, "err", headers or {}, io.BytesIO(b"{}"))


class FakeHubSpot:
    def __init__(self, routes, fail_once=None, forbid=()):
        self.routes, self.fail_once, self.forbid = routes, dict(fail_once or {}), set(forbid)
        self.calls = []

    def __call__(self, req, timeout):
        url = urllib.parse.urlparse(req.full_url)
        q = urllib.parse.parse_qs(url.query)
        self.calls.append((url.path, q))
        assert req.get_header("Authorization") == "Bearer tok"
        if url.path in self.forbid:
            raise _http_error(req.full_url, 403)
        if self.fail_once.pop(url.path, None):
            raise _http_error(req.full_url, 429, {"Retry-After": "0"})
        pages = self.routes[url.path]
        page = pages[int(q.get("after", ["0"])[0])]
        return json.dumps(page).encode()


def _routes():
    contact = lambda i, email: {"id": str(i), "properties": {  # noqa: E731
        "createdate": "2026-01-01T00:00:00Z", "hs_analytics_source": "ORGANIC_SEARCH", "email": email},
        "associations": {"companies": {"results": [
            {"id": "200", "type": "contact_to_company_unlabeled"}, {"id": "100", "type": "contact_to_company"},
            {"id": "100", "type": "contact_to_company_unlabeled"}]}}}
    return {
        "/crm/v3/objects/contacts": [
            {"results": [contact(1, "Pat@Example.com ")], "paging": {"next": {"after": "1"}}},
            {"results": [contact(2, None)]},
        ],
        "/crm/v3/objects/companies": [{"results": [{"id": "100", "properties": {"domain": "https://www.Example.com/"}}]}],
        "/crm/v3/objects/deals": [{"results": [{"id": "9", "properties": {"hs_is_closed_won": "true", "amount": "10"},
                                               "associations": {"contacts": {"results": [{"id": "1", "type": "deal_to_contact"}]}}}]}],
        "/crm/v3/pipelines/deals": [{"results": [{"id": "default", "label": "Sponsorship", "stages": []}]}],
        "/marketing/v3/campaigns": [{"results": []}],
        "/marketing/v3/emails": [{"results": []}],
    }


def test_pull_paginates_retries_and_hashes_email():
    fake = FakeHubSpot(_routes(), fail_once={"/crm/v3/objects/deals": True})
    out = hubspot.pull("tok", hash_salt="s3cret", fetch=fake, sleep=lambda s: None)

    assert [c["id"] for c in out["contacts"]] == ["1", "2"]                     # both pages
    c1 = out["contacts"][0]
    assert "email" not in c1["properties"]                                      # raw email gone
    assert c1["properties"]["email_hash"] == hubspot.hash_email("pat@example.com", "s3cret")
    assert out["contacts"][1]["properties"]["email_hash"] is None
    assert c1["associations"]["companies"] == ["100", "200"]                    # primary first, deduped
    assert len(out["deals"]) == 1                                               # survived a 429
    assert out["pipelines"][0]["label"] == "Sponsorship"
    assert out["_warnings"] == []
    # Never asks for names, phones, or deal names
    requested = {p for path, q in fake.calls for p in q.get("properties", [])}
    assert not requested & {"firstname", "lastname", "phone", "address", "dealname"}
    assert "Pat@Example.com" not in json.dumps(out)


def test_missing_optional_scope_is_a_warning_and_required_scope_is_an_error():
    out = hubspot.pull("tok", "s", fetch=FakeHubSpot(_routes(), forbid={"/marketing/v3/campaigns"}), sleep=lambda s: None)
    assert out["campaigns"] == [] and any("campaigns" in w and "403" in w for w in out["_warnings"])

    with pytest.raises(hubspot.HubSpotError, match="crm.objects.deals.read"):
        hubspot.pull("tok", "s", fetch=FakeHubSpot(_routes(), forbid={"/crm/v3/objects/deals"}), sleep=lambda s: None)


def test_bad_token_message():
    def fetch(req, timeout):
        raise _http_error(req.full_url, 401)
    with pytest.raises(hubspot.HubSpotError, match="HUBSPOT_TOKEN"):
        hubspot.pull("tok", "s", fetch=fetch, sleep=lambda s: None)


def test_no_salt_warns():
    out = hubspot.pull("tok", "", fetch=FakeHubSpot(_routes()), sleep=lambda s: None)
    assert all(c["properties"]["email_hash"] is None for c in out["contacts"])
    assert any("HASH_SALT" in w for w in out["_warnings"])


def test_pulled_data_normalizes_and_validates(tmp_path):
    from pipeline.transform.normalize import build_db
    from pipeline.validate import validate

    raw = hubspot.pull("tok", "s", fetch=FakeHubSpot(_routes()), sleep=lambda s: None)
    con = build_db(raw, tmp_path / "w.sqlite")
    assert con.execute("SELECT domain FROM companies").fetchone()[0] == "example.com"
    assert con.execute("SELECT company_id FROM contacts WHERE contact_id='1'").fetchone()[0] == "100"
    report = validate(con, raw)
    assert report["status"] in ("ok", "warning")
    assert any(c["id"] == "fill.email_hash" and c["value"] == 1 for c in report["checks"])


def test_validation_errors_block_analysis(tmp_path):
    from pipeline.transform.normalize import build_db
    from pipeline.validate import validate

    report = validate(build_db({"contacts": []}, tmp_path / "w.sqlite"), {})
    assert report["status"] == "error"
