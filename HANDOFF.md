# HANDOFF — INDATEL Sales & Marketing Insights

**Read this first.** It says where the project stands, what exists, where it lives, and
what to do next. Keep it current: update the "Last updated" line and the sections below
whenever something material changes.

**Last updated:** November 11, 2026
**Client:** INDATEL Services (work done under NDA, on INDATEL's Claude team account, with
HubSpot access granted by INDATEL)
**Project lead:** _(fill in)_ · **INDATEL contacts:** _(fill in: HubSpot admin, Salesforce admin, sponsor/marketing owner)_

---

## 1. What this is

An analytics pipeline and dashboard for INDATEL's HubSpot (and later Salesforce) data:
pull the data → compute metrics with confidence ranges and significance tests → Claude
writes findings → code checks every number and a second Claude pass reviews the reasoning
→ dashboard with charts, plain-English findings, and named **action lists** for the sales
and marketing team.

Original whiteboard plan: HubSpot pull → parser/organizer → Claude analytics (CLI on a
VPS/Azure container) → Claude double-check → dashboard API → legible dashboard. All of
those stages exist and work.

## 2. Where things are

| What | Where |
|---|---|
| Code, docs, tests | This repo: `github.com/doradodevs/indamothefuckindata` (branch `main`) |
| Project status detail | [`docs/STATUS.md`](docs/STATUS.md) |
| Architecture, data handling, field list for compliance | [`docs/DESIGN.md`](docs/DESIGN.md) |
| Azure deployment guide (draft) | [`docs/DEPLOY.md`](docs/DEPLOY.md) |
| Settings | [`.env.example`](.env.example) |
| **Real INDATEL data and deliverables** | **Not in this repo, by design.** See §4 |

Never commit INDATEL data (contacts, emails, deal values, exports, run folders) to this
repo. It lives on a personal GitHub account. `data/` is git-ignored.

## 3. Current state (what works today)

- **Pipeline end to end**, tested (44 tests, lint clean, CI on GitHub Actions).
  Sources: HubSpot API (`--source hubspot`), HubSpot CSV exports (`--source csv`), a raw
  JSON file (`--source raw`), and sample data (`--source fixtures`). Salesforce extractor
  and HubSpot↔Salesforce matching are built and tested on sample data, waiting on access.
- **Accuracy safeguards:** Python computes every number; 95% ranges; each group tested
  against the rest with a multiple-comparison adjustment; a code check rejects any
  number Claude can't trace to a metric; an independent Claude review rewords or rejects
  overstated findings.
- **Dashboard** (FastAPI serves it at `/`, or as a single self-contained HTML snapshot):
  Overview, **Action lists**, Lead sources, **Company types**, Industries, Forms &
  engagement, What moves together, All findings, Data health, How to read this.
- **First full run on real INDATEL HubSpot data: done (Nov 5, 2026)** via the Claude
  HubSpot connector (see §4). Findings in §5.

## 4. The real-data run: how it was done and where the outputs are

The HubSpot private-app token isn't issued yet, so the data was pulled through the
**Claude HubSpot connector** in a Claude Code session:

- All 4,449 contacts (30 properties each, 200 per page via `search_crm_objects`, sorted by
  `hs_object_id`), 1,608 companies, 22 deals (each deal's contacts and companies looked up
  one by one with `associatedWith` filters), and the 20 HubSpot owners.
- Assembled into the pipeline's raw JSON shape and run with `--source raw`.
- Settings used: `CONVERTED_STAGES=customer,2294533870` (Active, Inactive),
  `INCLUDE_CONTACT_DETAILS=true`, `INTERNAL_DOMAINS=indatel.com,indatelservices.com`,
  `HUBSPOT_PORTAL_ID=243832318`, `KEY_SEGMENTS=Member,Customer,Tail Provider,Supplier Affiliate`,
  `EXCLUDE_CONTACT_IDS=240925008600,240924595920,356958114513,319701388012` (2 HubSpot sample
  contacts, 2 test records), plus an `ANALYSIS_CONTEXT` file describing INDATEL's business.

**Where the outputs are:** the raw pull, warehouse, and dashboard file were in that
session's temporary workspace, which is wiped when the session ends. The deliverables
were sent to the project lead in chat:
- `INDATEL-Sales-Marketing-Insights.html` — full dashboard snapshot (open in Chrome/Edge;
  the in-app preview doesn't render it).
- `INDATEL-HubSpot-Review.pptx` and `.pdf` — 14-slide summary deck for INDATEL (findings,
  charts, recommendations, what we need from them). Summary level: no individual contact
  names. Built with pptxgenjs from the run's metrics; the generator script stayed in the
  session (it embeds INDATEL figures, so it isn't committed). To refresh it, rebuild from
  the dashboard numbers of the newest run.

**To redo the pull** (until the token exists): open a Claude Code session with the
HubSpot connector, repeat the steps above, write the JSON to a local file outside the repo,
and run `python -m pipeline.run --source raw --raw-file <file>` with the settings above,
then `python -m pipeline.publish.export_static --out <file>.html`. Once the token exists,
use `--source hubspot` and none of this is needed.

Contacts can't be linked to companies through HubSpot associations with the current
user's access (the property comes back empty), so the pipeline matches contacts to
companies by **email domain**. That gives company type for about 4 in 10 contacts.

## 5. What the data says (Nov 5, 2026 run)

Internal draft; to be reviewed with INDATEL before acting on it.

- **Website inquiries aren't being followed up (or it isn't logged):** 169 people
  filled in a website form; 6.5% have any sales activity logged in HubSpot. 151 are
  waiting, 15 of them from the last 30 days. Most came through the general Contact form.
- **Most contacts never receive marketing email:** 32% (1,403 of 4,395) have ever been
  sent one. Of those, 28% opened and 10% clicked.
- **By company type:** Members are the most engaged (75% emailed, 36% open). Customers
  are under-reached and under-engaged (40% emailed, 14% open). Tail Providers (7%) and
  Non-Members (4%) are barely emailed. 319 Member/Customer/Tail Provider/Supplier Affiliate
  contacts have never been sent marketing email.
- **"Active" status comes from sponsorship:** 11 of 12 people who started with the
  Business Symposium Sponsorship Application reached Active; 3 of 117 Contact-form
  inquirers did. Supplier Affiliates reach Active at 27% vs about 1% overall.
- **Sponsorships:** $198,500 won from 21 of 22 deals across 20 companies (mostly Supplier
  Affiliates). Premier, Diamond and Platinum are ~80% of the value.
- **Data foundations:** 93% of contacts have no owner; no contact has campaign attribution;
  deal close dates were bulk-edited (9 deals share one timestamp, 7 another); 50 INDATEL
  staff records sit among contacts (excluded from analysis); 366 records need cleanup.

### Week 2 (Nov 11, 2026 run)

Second pull, compared record by record with Nov 5 (details in `docs/STATUS.md`). Almost
nothing changed: +4 contacts, +1 company, no stage/deal changes, no marketing email sent,
no cleanup done, 1 inquiry followed up (152 now waiting). The Week 2 PDF report for INDATEL
lists every data hole with a trust rating and the access, definitions, and process changes
we need. Its generator (`report.html` + Playwright `page.pdf`) lives in the session
workspace, like the deck. To redo it, rerun the pull, diff against the previous week's
JSON, and update the numbers.

### Living report (v3, Nov 11, 2026)

The Nov 5 deck, the Week 2 review and INDATEL's Nov 11 responses are merged into one
versioned PDF ("INDATEL-Insights-Living-Report-v3.pdf"). Section 1 is a status tracker for
every request; Section 3 is a weekly log that gets a new column each pull. Update and
re-issue it as v4, v5, and so on, rather than starting new documents. INDATEL's Nov 11 responses:
token created (to be shared privately; store as `HUBSPOT_TOKEN`); Salesforce scope to be
discussed on the call; "Active" means different things by account type (report it per
type); dashboard hosting queued behind INDAContract on the INDAData instance (John Tracey);
their integration assessment doc was referenced but not yet received.

## 6. Blocked on / waiting for

| # | What | From | Unblocks |
|---|---|---|---|
| 1 | HubSpot read-only **private app token** | INDATEL HubSpot admin | Automatic daily refresh; no more manual connector pulls |
| 2 | **Campaign + marketing email** read access | INDATEL HubSpot admin | Campaign and email performance |
| 3 | **Salesforce** read-only API access | INDATEL Salesforce admin | Marketing → service deals (code is ready) |
| 4 | Confirm what lifecycle **"Active"** means at INDATEL | INDATEL marketing/sales ops | Correct reading of the conversion findings |
| 5 | Compliance sign-off on fields in scope (`docs/DESIGN.md` §5) | INDATEL compliance | Production use |
| 6 | Hosting decision (Azure Container Apps + Entra ID proposed) | INDATEL IT | Putting the dashboard online |
| 7 | Whether sponsorship value belongs in headline totals | INDATEL leadership | Overview tile definitions |

## 7. Next steps (in order)

1. **Send INDATEL the summary deck** (PowerPoint/PDF of the dashboard) and walk them
   through the action lists. Ask items 4 and 7 above in that meeting.
2. **Get the token** (item 1), run `python -m pipeline.preflight --live`, then the first
   automatic pull: `python -m pipeline.run --source hubspot`.
3. **Put the dashboard online** for INDATEL staff (`docs/DEPLOY.md`; Azure + Entra ID
   sign-in so only INDATEL logins can see it, since it contains contact details).
4. **Connect Salesforce** (item 3) for the service-deal view.
5. Ideas not started: findings history (week over week), alerts when a run fails
   validation, website traffic source (HubSpot vs Google Analytics).

## 8. Working rules for this project

- INDATEL data stays out of GitHub. Deliverables go to the project lead or INDATEL directly.
- Dashboards and decks are **internal and confidential** (contact details, sponsorship
  pricing). Anything going to INDATEL members, customers, or sponsors gets human review first.
- Claude never sees names or emails; it works from aggregate metrics. Names appear only in
  the action lists, built by plain SQL.
- Don't quote a number in a finding that the pipeline didn't compute; the code check will
  reject it anyway.
- Update this file and `docs/STATUS.md` at the end of every working session.

## 9. Changelog

- **2026-11-11** — Second full HubSpot pull; week-over-week comparison; pipeline rerun
  (3 verified, 4 revised, 2 held back findings); checker accepts quoted sample sizes;
  Week 2 data review PDF (what changed, data holes, what we need) delivered to project lead.

- **2026-11-05** — Project built end to end on sample data; Salesforce integration,
  significance testing, CSV importer, ops tooling. First full run on real INDATEL HubSpot
  data via the connector; action lists, company-type segments, email reach, follow-up and
  import metrics added; dashboard snapshot and summary deck delivered to project lead.
