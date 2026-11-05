import json
import sqlite3

import pytest

from pipeline.analyze.schemas import Finding, QuotedNumber
from pipeline.extract.fixtures import generate
from pipeline.metrics.compute import Metric, compute, pearson
from pipeline.transform.normalize import build_db
from pipeline.verify.checks import check_finding

TINY = {
    "contacts": [
        {"id": "1", "properties": {"email": "a@example.com", "firstname": "A", "createdate": "2026-01-05T00:00:00Z",
                                   "lifecyclestage": "customer", "hs_analytics_source": "PAID_SEARCH",
                                   "hs_analytics_first_touch_converting_campaign": "c1"},
         "associations": {"companies": ["10"]}},
        {"id": "2", "properties": {"createdate": "2026-01-09T00:00:00Z", "lifecyclestage": "lead",
                                   "hs_analytics_source": "PAID_SEARCH",
                                   "hs_analytics_first_touch_converting_campaign": "c1"}},
        {"id": "3", "properties": {"createdate": "2026-02-01T00:00:00Z", "lifecyclestage": "opportunity",
                                   "hs_analytics_source": "ORGANIC_SEARCH"}},
    ],
    "companies": [{"id": "10", "properties": {"name": "Example Co", "industry": "Carrier"}}],
    "deals": [
        {"id": "d1", "properties": {"amount": "1000", "dealstage": "closedwon", "createdate": "2026-01-10T00:00:00Z",
                                    "closedate": "2026-02-10T00:00:00Z"},
         "associations": {"contacts": ["1"], "companies": ["10"]}},
        {"id": "d2", "properties": {"amount": "", "dealstage": "closedlost", "createdate": "2026-02-02T00:00:00Z",
                                    "closedate": "2026-03-01T00:00:00Z"},
         "associations": {"contacts": ["3"]}},
    ],
    "campaigns": [{"id": "c1", "properties": {"hs_name": "Example Campaign", "channel": "paid_search"}}],
    "email_stats": [], "web_traffic": [],
}


@pytest.fixture
def tiny(tmp_path):
    con = build_db(TINY, tmp_path / "w.sqlite")
    return con, {m.id: m for m in compute(con)}


def test_pii_never_reaches_warehouse(tmp_path):
    con = build_db(TINY, tmp_path / "w.sqlite")
    dump = "\n".join(con.iterdump())
    assert "a@example.com" not in dump
    cols = [r[1] for r in con.execute("PRAGMA table_info(contacts)")]
    assert not {"email", "firstname", "lastname", "phone"} & set(cols)


def test_metrics_on_hand_checked_data(tiny):
    _, m = tiny
    assert m["overview.contacts"].value == 3
    assert m["overview.deals_won"].value == 1
    assert m["overview.revenue_won"].value == 1000.0
    assert m["overview.win_rate"].value == 0.5
    assert m["campaign.c1.contacts"].value == 2
    assert m["campaign.c1.revenue_per_contact"].value == 500.0
    assert m["campaign.none.contacts"].value == 1
    assert m["quality.deals_missing_amount_rate"].value == 0.5
    assert m["source.PAID_SEARCH.median_days_to_won"].value == 36.0


def test_pearson():
    assert pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert pearson([1, 2], [1, 2]) is None


def test_fixtures_contain_planted_patterns(tmp_path):
    con = build_db(generate(), tmp_path / "w.sqlite")
    m = {x.id: x for x in compute(con)}
    assert m["corr.sessions_vs_new_contacts.lag1"].value > 0.8
    assert m["campaign.cmp-001.revenue_per_contact"].value > m["campaign.cmp-002.revenue_per_contact"].value
    assert m["campaign.cmp-005.deals_won"].value <= 2


# --- verifier -------------------------------------------------------------

METRICS = {
    "campaign.c1.win_rate": Metric("campaign.c1.win_rate", 0.2513, "ratio", 200, ""),
    "campaign.c1.revenue_won": Metric("campaign.c1.revenue_won", 302400.0, "usd", 200, ""),
    "campaign.c2.win_rate": Metric("campaign.c2.win_rate", 0.6, "ratio", 5, ""),
}


def _f(claim, quoted, evidence=None, caveats=()):
    return Finding(finding_id="F1", title="t", type="pattern", claim=claim,
                   evidence=evidence or [q[1] for q in quoted],
                   quoted_numbers=[QuotedNumber(value=v, metric_id=i) for v, i in quoted],
                   confidence="high", caveats=list(caveats))


def test_verifier_accepts_correct_numbers_and_percent_rendering():
    f = _f("Win rate was 25.1% on $302,400 of revenue.",
           [(0.2513, "campaign.c1.win_rate"), (302400, "campaign.c1.revenue_won")])
    r = check_finding(f, METRICS, min_sample=20, tol=0.01)
    assert r.passed, r.errors


def test_verifier_rejects_wrong_quoted_number():
    r = check_finding(_f("Win rate was 31%.", [(0.31, "campaign.c1.win_rate")]), METRICS, 20, 0.01)
    assert not r.passed


def test_verifier_rejects_unbacked_number_in_claim():
    # Claude invents a derived number ("3x") that no metric supports.
    f = _f("Revenue of $302,400 is 3.5x the other campaign.", [(302400, "campaign.c1.revenue_won")])
    r = check_finding(f, METRICS, 20, 0.01)
    assert not r.passed and any("3.5" in e for e in r.errors)


def test_verifier_rejects_unknown_metric():
    r = check_finding(_f("x", [(1, "campaign.zzz.win_rate")]), METRICS, 20, 0.01)
    assert not r.passed


def test_verifier_requires_small_sample_caveat():
    f = _f("Win rate was 60%.", [(0.6, "campaign.c2.win_rate")])
    assert not check_finding(f, METRICS, 20, 0.01).passed
    f2 = _f("Win rate was 60%.", [(0.6, "campaign.c2.win_rate")], caveats=["Small sample: only 5 closed deals."])
    assert check_finding(f2, METRICS, 20, 0.01).passed


# --- API ------------------------------------------------------------------

def test_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import pipeline.publish.api as api
    from pipeline.config import Config

    run = tmp_path / "runs" / "20261105T000000Z"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({"run_id": run.name}))
    (run / "metrics.json").write_text(json.dumps([{"id": "campaign.c1.x", "value": 1}, {"id": "overview.y", "value": 2}]))
    (run / "findings.json").write_text(json.dumps({"summary": "s", "findings": [
        {"finding_id": "F1", "status": "verified"}, {"finding_id": "F2", "status": "rejected"}]}))
    monkeypatch.setattr(api, "cfg", Config(data_dir=tmp_path, api_token="secret"))
    c = TestClient(api.app)
    h = {"Authorization": "Bearer secret"}

    assert c.get("/health").status_code == 200
    assert c.get("/runs/latest").status_code == 401
    assert c.get("/runs/latest", headers=h).json()["run_id"] == run.name
    assert len(c.get("/runs/latest/metrics?prefix=campaign.", headers=h).json()) == 1
    assert [f["finding_id"] for f in c.get("/runs/latest/findings", headers=h).json()["findings"]] == ["F1"]
    assert len(c.get("/runs/latest/findings?status=all", headers=h).json()["findings"]) == 2
    assert c.get("/runs/..%2Fetc/metrics", headers=h).status_code in (400, 404)


# --- dashboard queries ------------------------------------------------------

def test_explore_activity_cohort_and_series(tmp_path):
    from pipeline.publish import explore

    con = build_db(TINY, tmp_path / "w.sqlite")
    con.close()
    wh = tmp_path / "w.sqlite"

    jan = explore.activity(wh, "2026-01", "2026-01")
    assert jan["new_contacts"] == 2 and jan["deals_won"] == 0
    feb = explore.activity(wh, "2026-02", "2026-02")
    assert feb["deals_won"] == 1 and feb["won_value"] == 1000.0 and feb["win_rate"] == 1.0

    # Cohort: January's contacts get credit for the deal that closed in February.
    cohort = {m["id"]: m["value"] for m in explore.cohort_metrics(wh, "2026-01", "2026-01", ("campaign.",))}
    assert cohort["campaign.c1.revenue_won"] == 1000.0
    assert "campaign.none.contacts" not in cohort  # contact 3 joined in February

    ser = explore.series(wh, "won_value", "campaign", "2026-01", "2026-03")
    assert ser["months"] == ["2026-01", "2026-02", "2026-03"]
    assert ser["series"] == [{"key": "c1", "values": [0.0, 1000.0, 0.0]}]

    assert explore.previous_window("2026-01", "2026-03") == ("2025-10", "2025-12")
    with pytest.raises(ValueError):
        explore.series(wh, "won_value", "campaign; DROP TABLE deals", "2026-01", "2026-03")
    with pytest.raises(ValueError):
        explore.cohort_metrics(wh, "2026-01' OR 1=1 --", "2026-02", ("campaign.",))


def test_dashboard_served(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import pipeline.publish.api as api
    from pipeline.config import Config

    monkeypatch.setattr(api, "cfg", Config(data_dir=tmp_path, api_token=None))
    r = TestClient(api.app).get("/")
    assert r.status_code == 200 and "Sales &amp; Marketing Insights" in r.text


def test_real_account_shapes(tmp_path):
    """Migrated pipelines use GUID stage IDs; HubSpot's own closed/won flags must win.
    Sample contacts are dropped; sales and sponsorship pipelines stay separate."""
    raw = {
        "contacts": [
            {"id": "1", "properties": {"createdate": "2026-01-05T00:00:00Z"}},
            {"id": "999", "properties": {"createdate": "2025-09-10T00:00:00Z"}},  # HubSpot sample contact
        ],
        "deals": [
            {"id": "d1", "properties": {"dealstage": "8d3f82d4-guid", "pipeline": "sf-guid", "hs_is_closed_won": "true",
                                        "hs_is_closed": "true", "amount": "100", "amount_in_home_currency": "120",
                                        "closedate": "2026-02-01T00:00:00Z"}, "associations": {"contacts": ["1"]}},
            {"id": "d2", "properties": {"dealstage": "closedwon", "pipeline": "default", "amount": "50",
                                        "closedate": "2026-02-01T00:00:00Z"}, "associations": {"contacts": ["1"]}},
        ],
        "pipelines": [{"id": "sf-guid", "label": "Sales"}, {"id": "default", "label": "Sponsorship"}],
    }
    con = build_db(raw, tmp_path / "w.sqlite", exclude_contact_ids=frozenset({"999"}))
    m = {x.id: x for x in compute(con)}
    assert m["overview.contacts"].value == 1
    assert m["pipeline.sf-guid.deals_won"].value == 1
    assert m["pipeline.sf-guid.revenue_won"].value == 120.0  # home-currency amount preferred
    assert m["pipeline.default.revenue_won"].value == 50.0   # falls back to stage name when flags absent
    assert "Sales" in m["pipeline.sf-guid.deals"].description


def test_verifier_requires_test_for_group_comparisons():
    mets = {
        "campaign.a.win_rate": Metric("campaign.a.win_rate", 0.5, "ratio", 100, "", 0.4, 0.6),
        "campaign.b.win_rate": Metric("campaign.b.win_rate", 0.3, "ratio", 100, "", 0.21, 0.4),
        "test.campaign.a.win_rate": Metric("test.campaign.a.win_rate", 0.2, "ratio", 100, "", p_value=0.01, significant=True),
        "test.campaign.b.win_rate": Metric("test.campaign.b.win_rate", -0.2, "ratio", 100, "", p_value=0.2, significant=False),
    }
    base = [(0.5, "campaign.a.win_rate"), (0.3, "campaign.b.win_rate")]
    # Comparison with no test and no caveat -> rejected
    assert not check_finding(_f("A wins 50%, B wins 30%.", base), mets, 20, 0.01).passed
    # Backed by a significant test -> ok
    ok = _f("A wins 50%, B wins 30%.", base, evidence=[q[1] for q in base] + ["test.campaign.a.win_rate"])
    assert check_finding(ok, mets, 20, 0.01).passed
    # Only a non-significant test but honest caveat -> ok
    hedged = _f("A wins 50%, B wins 30%.", base, caveats=["The gap could be chance."])
    assert check_finding(hedged, mets, 20, 0.01).passed
    # Claims significance on a non-significant test -> rejected
    bad = _f("B is significantly lower.", [(0.3, "campaign.b.win_rate")],
             evidence=["campaign.b.win_rate", "test.campaign.b.win_rate"])
    assert not check_finding(bad, mets, 20, 0.01).passed
    # Interval ends of a cited metric may appear in the text
    rng = _f("A wins 50% (likely between 40% and 60%).", [(0.5, "campaign.a.win_rate")])
    assert check_finding(rng, mets, 20, 0.01).passed
