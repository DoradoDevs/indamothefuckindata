"""HubSpot UI export files -> same raw shape as the API."""
from pipeline.extract.csv_import import load_dir
from pipeline.extract.hubspot import hash_email
from pipeline.metrics.compute import compute
from pipeline.transform.normalize import build_db

CONTACTS = (
    "﻿Record ID,First Name,Last Name,Email,Phone Number,Create Date,Lifecycle Stage,Original Source,"
    "First touch converting campaign,Associated Company IDs (Primary)\n"
    "101,Pat,Doe,pat@example.com,555-0100,2026-01-05 14:03,Marketing Qualified Lead,Organic Search,Webinar,9001\n"
    "102,Sam,Roe,,555-0101,2026-02-10 09:00,Customer,Offline Sources,,9002\n"
)
COMPANIES = "Record ID,Company name,Company Domain Name,Industry,State/Region,Create Date\n" \
            "9001,Example Co,example.com,Telecommunications,IA,2025-12-01\n" \
            "9002,Sample LLC,sample.org,,KS,2025-12-02\n"
DEALS = ("Record ID,Deal Name,Deal Stage,Pipeline,Amount,Create Date,Close Date,Associated Contact IDs,"
         "Associated Company IDs\n"
         "5001,Gold - Example,Closed Won,Sponsorship Pipeline,\"$7,500\",2026-01-10,2026-02-01,101,9001\n"
         "5002,Silver - Sample,Closed Lost,Sponsorship Pipeline,2500,2026-02-11,2026-03-01,102;103,9002\n"
         "5003,Bronze,Interest Shown,Sponsorship Pipeline,,2026-03-01,,102,9002\n")


def test_loads_exports_and_drops_personal_columns(tmp_path):
    (tmp_path / "export-a.csv").write_text(CONTACTS, encoding="utf-8")
    (tmp_path / "hubspot-crm-exports-companies.csv").write_text(COMPANIES)
    (tmp_path / "x.csv").write_text(DEALS)
    (tmp_path / "notes.txt").write_text("ignored")
    raw = load_dir(tmp_path, hash_salt="salt")

    assert len(raw["contacts"]) == 2 and len(raw["companies"]) == 2 and len(raw["deals"]) == 3
    c = raw["contacts"][0]["properties"]
    assert c["hs_analytics_source"] == "ORGANIC_SEARCH"
    assert c["lifecyclestage"] == "marketingqualifiedlead"
    assert c["email_hash"] == hash_email("pat@example.com", "salt")
    dump = repr(raw)
    for personal in ("Pat", "Doe", "pat@example.com", "555-0100", "Gold - Example"):
        assert personal not in dump
    d = {x["id"]: x["properties"] for x in raw["deals"]}
    assert d["5001"]["hs_is_closed_won"] == "true" and d["5001"]["amount"] == "7500"
    assert d["5002"]["hs_is_closed"] == "true" and d["5002"]["hs_is_closed_won"] == "false"
    assert d["5003"]["hs_is_closed"] == "false"
    assert raw["deals"][1]["associations"]["contacts"] == ["102", "103"]
    assert raw["pipelines"] == [{"id": "Sponsorship Pipeline", "label": "Sponsorship Pipeline", "stages": []}]

    con = build_db(raw, tmp_path / "w.sqlite")
    m = {x.id: x for x in compute(con)}
    assert m["pipeline.Sponsorship Pipeline.revenue_won"].value == 7500.0
    assert m["overview.contacts"].value == 2


def test_unknown_file_is_skipped_with_warning(tmp_path):
    (tmp_path / "mystery.csv").write_text("foo,bar\n1,2\n")
    (tmp_path / "c.csv").write_text(CONTACTS)
    raw = load_dir(tmp_path)
    assert any("mystery.csv" in w for w in raw["_warnings"])
    assert any("HASH_SALT" in w for w in raw["_warnings"])
