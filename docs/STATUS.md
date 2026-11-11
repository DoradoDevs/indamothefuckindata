# Project status

**Last updated:** November 11, 2026
**State:** Built and tested end to end on sample data, including Salesforce. **Waiting on
data access** for the first real run. On the day access lands, run
`python -m pipeline.preflight --live` and follow the checklist below.

Nothing in this repo contains real INDATEL data. All numbers in screenshots, tests, and
sample runs come from the synthetic generators in `pipeline/extract/fixtures.py`.

## Waiting on

| # | Access | From | Why we need it | Unblocks |
|---|---|---|---|---|
| 1 | **HubSpot read-only private app token** (contacts, companies, deals, campaigns, marketing emails) | INDATEL HubSpot admin | The scheduled pipeline pulls data with it. The chat connector isn't for bulk pulls, and the current user has no export permission. | First real run; daily refresh |
| 2 | **HubSpot campaign + marketing email read permission** on the project user | INDATEL HubSpot admin | Campaign and email results are blocked for the current user. | Looking at campaign data together via the connector |
| 3 | **Salesforce read-only API access**: a connected app with the client credentials flow and a read-only integration user (Opportunity, OpportunityContactRole, Account, Contact) | INDATEL Salesforce admin | Telecom service sales appear to live in Salesforce. HubSpot only holds event sponsorship deals. | "Which marketing brings in money" (Service deals page) |
| 4 | **Compliance sign-off** on the fields listed in [DESIGN.md §5](DESIGN.md#5-data-handling-needs-review) | INDATEL compliance | Customer records and deal values are confidential; circuit details, service locations, and call records must stay out. | First real pull |
| 5 | **Hosting approval** (Azure Container Apps + Entra ID sign-in, see [DEPLOY.md](DEPLOY.md)) | INDATEL IT | Where it runs and how staff log in. | Sharing the dashboard |

## The day access lands

1. **Set secrets** (local `.env` or Azure Key Vault): `HUBSPOT_TOKEN`, `HASH_SALT` (generate
   once, never change), `EXCLUDE_CONTACT_IDS` (HubSpot's two sample contacts),
   `CLAUDE_CODE_OAUTH_TOKEN`, and, when available, `SF_LOGIN_URL`, `SF_CLIENT_ID`,
   `SF_CLIENT_SECRET`.
2. **Run preflight:** `python -m pipeline.preflight --live`. It checks every setting and
   makes one tiny read-only call per object to confirm the token's scopes. Fix anything
   marked ✗.
3. **First pull without Claude:** `python -m pipeline.run --source hubspot --skip-claude`.
   Open the dashboard's **Data health** page and compare record counts with HubSpot.
4. **Fix what validation flags.** Items marked `VERIFY` in the extractors (campaign
   property format, marketing email counters, association paging) are the likely spots.
5. **Full run with Claude**, then read every finding yourself before anyone else sees the
   dashboard.
6. **Deploy** per [DEPLOY.md](DEPLOY.md) once hosting is approved.

If HubSpot CSV exports turn up before the token: `python -m pipeline.run --source csv
--csv-dir <folder>`.

## Done

- **Pipeline end to end:** extract → normalize (personal fields dropped) → validate →
  metrics → Claude analysis → code check of every number → independent Claude review → API
  → dashboard.
- **Accuracy safeguards:**
  - Python computes every number; Claude only interprets.
  - Every rate has a 95% likely range. Every campaign, source, and industry is tested
    against the rest, with an adjustment for testing many groups at once.
  - The code check rejects any number not backed by a metric, group comparisons without a
    significant test or a "could be chance" caveat, and "significant" claims without a test.
  - Statistics cross-checked against SciPy and locked in as tests.
  - On sample data, real patterns test as significant, and the random industry
    difference that slipped through earlier now correctly doesn't.
- **HubSpot extractor**, hardened: retries, clear errors for a bad token or missing scope,
  optional data degrades to warnings, no names/phones/addresses/deal names requested,
  email turned into a keyed one-way hash in memory.
- **Salesforce integration** (built, waiting on access): read-only pull, matching to
  HubSpot by hashed email then company domain, marketing credited only if it reached the
  customer before the deal started, service-deal metrics with tests, **Service deals**
  dashboard page.
- **CSV importer** for HubSpot UI exports.
- **Pull validation report** on every run; a run that fails validation never replaces the
  dashboard's last good run.
- **Dashboard** for sales staff and leadership: time-period filter with period-over-period
  comparison; overview, campaigns, lead sources, industries, service deals, "what moves
  together", all findings, data health, and plain-English definitions. Likely ranges and a
  "Stands out?" column say what's real and what could be luck. Works on phones and in dark
  mode.
- **Operations:** preflight checklist, data retention (`python -m pipeline.prune`), run
  lock, runs published atomically (safe on Azure Files), GitHub Actions CI, hardened
  Dockerfile, Azure deployment guide.
- **39 automated tests**, lint clean.

## Second run on real HubSpot data (November 11, 2026)

Same connector method and settings as Nov 5; compared record by record with the Nov 5 pull.

- 4,453 contacts (+4, none deleted), 1,609 companies (+1), 22 deals (unchanged), access unchanged; tickets: 0.
- No lifecycle changes, no deal changes, no marketing email delivered to any contact, 0 of 366 cleanup records fixed.
- 1 of the 151 waiting website inquiries got logged follow-up; 2 new inquiries, so 152 waiting.
- New data-quality facts: 267 contacts owned by the "Hubspot Automations" system user; all 22 deals (and 22 contacts) owned by a deactivated user; companies report 3,371 linked contacts that our access can't read; 19 company names duplicated across 44 records; 89% of contacts created in the Nov 11, 2025 migration.
- Checker fix: a quoted sample size (n) now counts as backed by its metric (two findings had been held back for this).
- Deliverable: `INDATEL-HubSpot-Data-Review-Week2.pdf` (12 pages: what changed, findings, data holes with trust ratings, what we need), sent to the project lead.

## First run on real HubSpot data (November 5, 2026)

Run through the chat connector while the API token is pending: the 60 Active/Inactive and
166 website contacts read individually (analytics fields only, IDs scrambled, no names
stored), the ~4,200 imported prospects represented by exact monthly counts, plus all 22
sponsorship deals. The data stayed in the session's scratch space; nothing from it is in
this repo. The dashboard was shared as a self-contained snapshot file.

Result: 9 findings, none rejected by the code check, 4 confirmed and 5 reworded by the
reviewer. Key points: the sponsorship application form is where Active contacts come
from; general website inquiries (Contact form) rarely reach Active, likely because
service inquiries are tracked in Salesforce; the imported list holds most sponsorship
dollars but converts rarely; 87% of contacts have never engaged; campaign and company
links are missing everywhere; deal close dates were bulk-edited.

What the run added to the pipeline: goal-status conversion (`CONVERTED_STAGES`), email
and form engagement, first-form analysis, lifecycle stage labels, sponsorship tiers
(deal groups), a business-context file for Claude (`ANALYSIS_CONTEXT`), `--source raw`,
a static snapshot export (`python -m pipeline.publish.export_static`), and dashboard
fixes (no comparisons against partly covered periods, data notes on the Overview).

## Latest sample run (with Claude)

9 findings. The code check rejected none (every number traced to a metric). The reviewer
confirmed 2 and revised 7: for example, it noted that webinar sign-ups may already be
closer to buying (selection, not proof the webinar causes sales), and it flagged borderline
tests. About 2.5 minutes; roughly $0.80 of Claude usage at list price, which on the
subscription counts against usage limits instead of being billed.

## What we know about the live HubSpot account

From a read-only, summary-level look through the connector. Details in
[DESIGN.md §6](DESIGN.md#6-what-the-live-hubspot-account-looks-like-checked-via-the-connector).

- HubSpot is mainly a **marketing contact database plus event sponsorships**.
- The migrated sales pipeline is **empty**; service sales are likely in Salesforce.
- **Campaign attribution is almost never set** on contacts. Campaign results will stay
  thin until Marketing tags campaigns consistently. Worth raising with Marketing now.

## Still open (can do without access)

- **Sponsorship value in headline totals?** HubSpot's "won deal value" tiles currently add
  the sponsorship pipeline to the (empty) sales pipeline. Each pipeline is also reported
  separately. Decide whether headline totals should cover sales only; it's a small change.
- **Website traffic source:** HubSpot's traffic API is legacy and tier-dependent. Decide
  between HubSpot and Google Analytics; the "What moves together" page is ready for it.
- **Alerts** when a scheduled run fails validation (email or Teams), once hosting is decided.
- **Findings history:** compare this week's findings with last week's on the dashboard.

## Decisions made

| Decision | Choice |
|---|---|
| Accuracy approach | Python computes every number; Claude only interprets; code and a second Claude review check it |
| "Is this difference real?" | 95% ranges; each group vs. the rest; Benjamini-Hochberg adjustment; 5% threshold |
| Claude billing | Subscription via Claude Code CLI, not API key |
| "Revenue" on the dashboard | Labeled "won deal value" / "service value won" (booked value, not cash received) |
| Marketing credit for a service deal | Only if HubSpot reached the customer before the Salesforce opportunity was created |
| Matching HubSpot ↔ Salesforce | Keyed one-way hash of email (raw emails never stored), then company web domain |
| Dashboard audience | INDATEL sales staff and leadership, non-technical |
| Hosting | Leaning Azure Container Apps, pending INDATEL |
| Data handling | Personal fields dropped before analysis; no bulk copying via the chat connector; dashboard marked internal and confidential |
