# HubSpot Analytics Pipeline — Design (DRAFT)

Status: draft for internal review. Built and tested end to end on sample data,
including Salesforce; waiting on data access (see [STATUS.md](STATUS.md)). Built from the whiteboard sketch: HubSpot pull →
parser/organizer → Claude analytics → Claude double-check → dashboard API → dashboard.

## 1. Goal

Pull HubSpot CRM and marketing data on a schedule and answer questions like:

- Which marketing campaigns produce contacts, deals, and won revenue — and which don't?
- Which lead sources convert, and how fast?
- Do changes in website traffic show up later in new contacts or revenue?
- What changed since the last run (anomalies)?

The output is a set of **verified findings** plus the **metrics behind them**, served
over an HTTP API to a dashboard.

## 2. The core rule: Python computes, Claude interprets

Language models are good at spotting patterns and explaining them. They are not a
reliable calculator. So the pipeline splits the work:

| Job | Who does it | Why |
|---|---|---|
| Counting, sums, rates, correlations | Python (deterministic) | Repeatable, testable, exact |
| Spotting patterns, explaining them, suggesting next steps | Claude (analyst pass) | Judgment over many metrics |
| Checking every number Claude quotes | Python (`verify/checks.py`) | A quoted number must match a computed metric |
| Checking the reasoning (causation claims, thin samples) | Claude (reviewer pass, fresh context) | Second opinion that isn't anchored on the first |

Every metric has a stable ID (for example `campaign.cmp-003.win_rate`). Claude must
cite metric IDs for every number it uses. A finding whose numbers don't match its cited
metrics is rejected before a human ever sees it. That's what "accurate data" on the
sketch turns into in practice.

## 3. Stages

```
 ┌──────────────┐   raw JSON    ┌──────────────┐  SQLite +   ┌──────────────┐
 │ 1. Extract   │ ────────────▶ │ 2. Organize  │ agent pack  │ 3. Metrics   │
 │ HubSpot,     │  data/raw/    │ normalize,   │ ──────────▶ │ + ranges,    │
 │ Salesforce,  │  <run_id>/    │ strip PII,   │             │ tests        │
 │ CSV, sample  │               │ match, check │             │ metrics.json │
 └──────────────┘               └──────────────┘             └──────┬───────┘
                                                                    │
 ┌──────────────┐  findings     ┌──────────────┐  draft      ┌──────▼───────┐
 │ 6. Dashboard │ ◀──────────── │ 5. Publish   │ ◀────────── │ 4a. Analyst  │
 │ HTML/JS      │   HTTP API    │ FastAPI      │  verified   │ claude -p    │
 └──────────────┘               └──────────────┘  findings   ├──────────────┤
                                                             │ 4b. Verify   │
                                                             │ code + claude│
                                                             └──────────────┘
```

Each run gets a `run_id` (UTC timestamp). Every stage writes its output to
`data/runs/<run_id>/`, so any dashboard number can be traced back to the raw pull.

### 3.1 Extract (`pipeline/extract/`)

- `hubspot.py` — HubSpot CRM API v3 client. Pulls contacts, companies, deals, deal
  pipelines, campaigns, and marketing email stats, with pagination. Auth: a read-only
  private-app token in `HUBSPOT_TOKEN`, issued by the HubSpot admin. (The Claude HubSpot
  connector is for interactive lookups in chat, not bulk extraction; see §6.) Personal
  fields and deal names are never requested.
- `salesforce.py` — Salesforce service deals (§3.7). Built; waiting on access.
- `csv_import.py` — HubSpot UI export files, for use before the token exists.
- `fixtures.py` — generates a realistic synthetic dataset with fictional names so the
  whole pipeline can be built and tested before the connector is approved. The
  synthetic data contains a few planted patterns (one strong campaign, one that
  produces contacts but no revenue, a traffic→contacts lag) so we can confirm the
  analysis actually finds them.

Output: `raw/*.json`, exactly as received. Never modified after write.

### 3.2 Organize (`pipeline/transform/`)

- `normalize.py` — flattens raw records into tidy SQLite tables (`contacts`, `companies`,
  `deals`, `campaigns`, `email_stats`, `web_traffic`). Types are coerced, dates parsed,
  associations resolved.
- **PII stripping happens here.** Names, email addresses, phone numbers, and street
  addresses are dropped; contacts keep only an opaque ID, lifecycle stage, source,
  campaign, company, and dates. Nothing personal reaches the Claude stages.
- `agent_pack.py` — writes a compact Markdown + JSON bundle sized for Claude: the
  metrics table, data-quality notes (missing fields, date range, record counts), and
  the run-over-run deltas. Claude gets this pack, not the raw database.

### 3.3 Metrics (`pipeline/metrics/`)

Deterministic metrics, each with `id`, `value`, `unit`, `n` (sample size), and a
plain-English `description`. Initial set:

- Funnel: contacts by lifecycle stage; contact→deal and deal→won rates
- Campaigns: contacts, deals, won deals, won revenue, win rate, email open/click rate,
  revenue per contact
- Lead sources: same as campaigns, plus median days from contact created to deal won
- Time series: monthly new contacts, won revenue, sessions
- Lagged correlations: sessions vs. new contacts vs. won revenue at 0–2 month lags,
  with `n` so thin correlations are visible
- Deal pipelines: deals, wins, win rate, and won value per pipeline (sales and
  sponsorship are never summed by default)
- Data quality: % of deals with no amount, % of contacts with no source, campaign, or
  company
- Service deals (§3.7)

**Is a difference real?** (`metrics/stats.py`, no extra dependencies, cross-checked
against SciPy in tests)
- Every rate gets a 95% Wilson interval; means (revenue per contact) a normal interval;
  correlations a Fisher-z interval.
- Each campaign / source / industry is compared with all the others combined
  (`test.<dim>.<key>.<measure>`): Fisher's exact test for rates, Welch's test for means.
- P-values are adjusted with Benjamini-Hochberg within each family (e.g. "campaign win
  rates"), so comparing ten campaigns doesn't manufacture a winner. Threshold: 5%.
- The dashboard turns this into plain language: a likely-range whisker on bars and a
  "Stands out?" column ("Higher win rate", or "No clear difference").

### 3.4 Analyze + Verify (`pipeline/analyze/`, `pipeline/verify/`)

1. **Analyst pass** — `claude -p` with tools disabled, the agent pack on stdin, and
   `--json-schema` forcing the findings format. Each finding has: title, claim, type
   (pattern / anomaly / recommendation / data-quality), cited metric IDs, every
   quoted number paired with its metric ID, confidence, and caveats.
2. **Code check** (`verify/checks.py`):
   - every metric ID must exist;
   - every number in the claim must come from a cited metric (its value, its 95% range
     ends, its adjusted p-value, or the size of a signed difference), matched at the
     precision it was written to; invented numbers like "3.5x" are rejected;
   - findings on small samples (`n` below a threshold) must say so in caveats;
   - comparing groups needs a significant `test.*` metric or a "could be chance" caveat;
   - the words "significant"/"statistically" need a cited metric marked significant.
   Failures are rejected, with the reason recorded and shown on **All findings**.
3. **Reviewer pass** — a second `claude -p` in a fresh context gets the surviving
   findings and the full metrics table (so it can catch cherry-picking). It returns `confirmed` / `revise` / `reject` per finding,
   focused on causation-vs-correlation, cherry-picking, and missing caveats.
4. Only `confirmed` findings are published as verified. Everything else is kept in the
   run folder for audit, and can be shown on the dashboard behind a "rejected" filter.

If the `claude` CLI isn't available (local dev, CI), the analyze stage is skipped and
the run still publishes metrics.

### 3.5 Publish (`pipeline/publish/api.py`)

FastAPI, read-only:

- `GET /health`
- `GET /runs` — list of runs
- `GET /runs/latest` — summary of the latest run
- `GET /runs/{run_id}/metrics` (filter with `?prefix=campaign.`)
- `GET /runs/{run_id}/findings` (`?status=published|verified|revised|rejected|all`)
- `GET /runs/{run_id}/explore/options` — months, campaigns, sources, industries, pipelines
- `GET /runs/{run_id}/explore/summary?start=YYYY-MM&end=YYYY-MM&compare=true` — tiles and
  campaign/source/industry results for any month range, plus the previous period
- `GET /runs/{run_id}/explore/series?measure=…&by=…&start=…&end=…` — monthly trends

The dashboard only talks to this API. Auth is a bearer token for now (`API_TOKEN`);
swap for Entra ID / SSO if this runs in Azure.

### 3.6 Dashboard (`pipeline/publish/static/`)

Built for sales staff and leadership, not analysts. Plain HTML/JS with no build step,
served by the same FastAPI app at `/`, so one Azure container serves both.

- **One filter row** (time period presets or custom months, plus "compare with the
  period before") scopes every number on the page, so the numbers always agree.
- **Overview:** headline tiles (new contacts, deals created, deals won, won deal value,
  win rate, website visits) with change vs. the previous period; a month-by-month chart
  that can be split by campaign, source, or industry; and Claude's checked findings in
  plain language, with the supporting numbers one click away.
- **Campaigns / Lead sources / Industries:** side-by-side bars, the trend over time,
  and a sortable table with period-over-period changes. Groups that rest on fewer than
  20 finished deals are marked "small group".
- **What moves together:** lagged links (traffic -> contacts -> revenue) explained in
  words ("Strong link, 1 month later"), with aligned charts for checking by eye.
- **All findings:** includes the held-back ones and why, so the checking is visible.
- **Data health:** HubSpot data gaps that understate other numbers.
- **How to read this:** plain-English definitions.

Two kinds of numbers, labeled on the page:
- *Activity in the period* (tiles, trends): what happened in those months.
- *Results from people who came in during the period* (campaign/source/industry
  pages): the fair way to credit a campaign, since its deals may close months later.

Both reuse the pipeline's metric code (`publish/explore.py`), so the dashboard and
Claude's findings can't disagree on definitions.

### 3.7 Salesforce (built, pending access)

HubSpot holds marketing contacts and sponsorship deals; telecom service deals appear to
live in Salesforce.

- **Pull** (`extract/salesforce.py`): OAuth client credentials with a read-only integration
  user; SOQL for Opportunity, OpportunityContactRole, Account, Contact (fields in §5).
- **Match** (`transform/salesforce.py`), in order of confidence:
  1. opportunity contact role → Salesforce contact → HubSpot contact with the same keyed
     email hash (primary role first);
  2. otherwise Salesforce account → HubSpot company with the same web domain → the
     earliest HubSpot contact at that company.
- **Credit rule:** marketing gets credit for a service deal only if the matched HubSpot
  contact existed *before* the opportunity was created. Earlier deals are kept, not credited.
- **Metrics** (`metrics/service.py`): service deals won, value, win rate, share of won deals
  marketing reached first, days from first touch to win; by campaign and source, with
  ranges and tests. Activity numbers use close dates in the period; campaign/source numbers
  use contacts who came in during the period.
- **Dashboard:** "Service deals" page, shown only when Salesforce data exists.
- **Validation:** match rates by method; a warning if more than half of opportunities
  can't be matched.

## 4. Deployment and operations

Step-by-step Azure guide: [DEPLOY.md](DEPLOY.md). Tools:
- `python -m pipeline.preflight [--live]` — settings, secrets, and token-scope checklist.
- `python -m pipeline.prune` — raw pulls deleted after 30 days, runs after 180, newest 5 kept.
- Run lock (no overlapping runs); runs built in a hidden folder and published by rename;
  the API opens finished runs read-only and immutable (safe on an Azure Files share); the
  dashboard shows the newest run that passed validation.
- CI (GitHub Actions): lint, tests on Python 3.11 and 3.13, dashboard JS syntax, and a
  sample end-to-end run.

### Original sketch: VPS or Azure container

One container image, three entry points:

- `python -m pipeline.run` — run on a schedule (cron on a VPS, or an Azure Container
  Apps job on a timer). Needs `HUBSPOT_TOKEN` and Claude CLI auth
  (`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, i.e. the subscription).
- `uvicorn pipeline.publish.api:app` — the API, always on.
- `data/` on a mounted volume (Azure Files) so runs survive restarts.

Secrets live in the host's secret store (Azure Key Vault / Container Apps secrets),
never in the repo.

## 5. Data handling (needs review)

**Fields pulled** (for compliance review; nothing else is requested):

| System | Object | Fields |
|---|---|---|
| HubSpot | Contact | record ID, create/modified dates, lifecycle stage, original source, first-touch converting campaign, associated company; email (hashed in memory, raw value discarded) |
| HubSpot | Company | record ID, name, web domain, industry, state, create date |
| HubSpot | Deal | record ID, amount, stage, pipeline, create/close dates, closed/won flags, associated contact and company |
| HubSpot | Campaign / marketing email | campaign ID, name, dates; email send/open/click counts |
| Salesforce | Opportunity | ID, account ID, amount, stage, closed/won flags, create/close dates, type, lead source |
| Salesforce | OpportunityContactRole | opportunity ID, contact ID, primary flag |
| Salesforce | Account | ID, website, industry, billing state, type |
| Salesforce | Contact | ID, account ID, create date, lead source; email (hashed in memory, raw value discarded) |

Never requested: names of people, phone numbers, street addresses, deal/opportunity
names, descriptions, notes, and any product, circuit, or service-location fields.

Email hashing: HMAC-SHA256 of the lowercased email with a secret key (`HASH_SALT`) held
in the secret store. The same key is used for both systems so they can be matched; the
hash can't be reversed without the key, and raw emails are never written anywhere.

- HubSpot contact records are personal data. The pipeline drops PII in stage 2, before
  anything reaches Claude or the API. Raw pulls (which do contain PII) stay on the
  pipeline host and should get a retention limit.
- Deal names are not pulled (they often contain customer names).
- No bulk copying of HubSpot records through the chat connector; bulk data comes only
  through the admin-issued token.
- Deal amounts are financial data. The dashboard should be internal-only. Nothing from
  this pipeline should go into member- or customer-facing material without review.
- If HubSpot holds any circuit details, service locations, or call records, those are
  CPNI-adjacent and should be excluded at extract time. **Please confirm with
  compliance which HubSpot properties are in scope before the first real pull.**

## 6. What the live HubSpot account looks like (checked via the connector)

Read-only look, summary level only; no records copied.

- The account was set up in 2025 and includes a deal pipeline migrated from Salesforce.
  Its stage IDs are GUIDs, so wins are detected with HubSpot's `hs_is_closed_won` /
  `hs_is_closed` flags rather than stage names.
- Two deal pipelines: a sales pipeline (currently empty) and a sponsorship pipeline
  (event sponsorships). They are reported separately and never summed by default.
- Telecom service sales appear to live in Salesforce, not HubSpot. "Money in" analysis
  will need Salesforce opportunity data joined to HubSpot contacts/companies.
  **Pending: Salesforce read access.**
- First-touch campaign attribution is almost never set on contacts, so campaign results
  will be thin until campaigns are tagged consistently in HubSpot.
- HubSpot's sample contacts exist; drop them via `EXCLUDE_CONTACT_IDS`.
- The connected user can read contacts, companies, and deals, but not campaigns or
  marketing emails, and has no export permission. The scheduled pipeline needs an
  admin-issued read-only private app token; the chat connector is not a substitute
  for bulk extraction.

## 7. Open questions

1. Which HubSpot hub/tier do we have? Still unknown (shown under **Settings** →
   **Account & Billing** → **Products & Add-ons**, admin access needed). Campaign
   attribution and traffic reporting depend on it.
2. Revenue: HubSpot only holds sponsorship deals. Service revenue needs Salesforce. On
   either system, confirm how Sales fills in Amount (monthly charge, annual, or total
   contract value). Cash actually received would need billing data, a later integration.
3. Is anyone in Marketing able to tag campaigns consistently in HubSpot going forward?
   Without it, campaign results stay thin.
4. How far back should the first backfill go?
5. Run frequency — daily is the default assumption.
6. Dashboard users: INDATEL sales staff and leadership (non-technical). (Decided.)
7. Hosting: leaning Azure (Container Apps + Azure Files + Key Vault), pending INDATEL's
   OK. Put Entra ID sign-in in front of the dashboard so staff use their work login.
8. Claude stages run on Claude Code CLI with subscription auth (decided). Watch usage
   limits as run frequency grows; the runner is isolated in one file if we ever move
   to the API.
