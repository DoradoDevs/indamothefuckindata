# HubSpot Analytics Pipeline — Design (DRAFT)

Status: draft for internal review. Built from the whiteboard sketch: HubSpot pull →
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
 │ HubSpot API  │  data/raw/    │ normalize,   │ ──────────▶ │ deterministic│
 │ or fixtures  │  <run_id>/    │ strip PII    │             │ metrics.json │
 └──────────────┘               └──────────────┘             └──────┬───────┘
                                                                    │
 ┌──────────────┐  findings     ┌──────────────┐  draft      ┌──────▼───────┐
 │ 6. Dashboard │ ◀──────────── │ 5. Publish   │ ◀────────── │ 4a. Analyst  │
 │ (TBD)        │   HTTP API    │ FastAPI      │  verified   │ claude -p    │
 └──────────────┘               └──────────────┘  findings   ├──────────────┤
                                                             │ 4b. Verify   │
                                                             │ code + claude│
                                                             └──────────────┘
```

Each run gets a `run_id` (UTC timestamp). Every stage writes its output to
`data/runs/<run_id>/`, so any dashboard number can be traced back to the raw pull.

### 3.1 Extract (`pipeline/extract/`)

- `hubspot.py` — HubSpot CRM API v3 client. Pulls contacts, companies, deals,
  campaigns, and marketing email stats, with pagination and incremental pulls by
  `lastmodifieddate`. Auth: private-app token in `HUBSPOT_TOKEN` (or the Claude
  HubSpot connector once it's approved — see §7).
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
- Data quality: % of deals with no amount, % of contacts with no source/campaign

### 3.4 Analyze + Verify (`pipeline/analyze/`, `pipeline/verify/`)

1. **Analyst pass** — `claude -p` with tools disabled, the agent pack on stdin, and
   `--json-schema` forcing the findings format. Each finding has: title, claim, type
   (pattern / anomaly / recommendation / data-quality), cited metric IDs, every
   quoted number paired with its metric ID, confidence, and caveats.
2. **Code check** — every metric ID must exist; every quoted number must match its
   metric (allowing for rounding); findings that rest on small samples (`n` below a
   threshold) must say so in caveats. Failures are rejected, with the reason recorded.
3. **Reviewer pass** — a second `claude -p` in a fresh context gets only the surviving
   findings and the metrics. It returns `confirmed` / `revise` / `reject` per finding,
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
- `GET /runs/{run_id}/findings` (`?status=verified|rejected|all`)

The dashboard only talks to this API. Auth is a bearer token for now (`API_TOKEN`);
swap for Entra ID / SSO if this runs in Azure.

### 3.6 Dashboard

Not started. Two options worth weighing once we know who uses it:
a small static page that reads the API, or an existing BI tool (Power BI reads JSON
APIs) if that's already in use.

## 4. Deployment (VPS or Azure container)

One container image, three entry points:

- `python -m pipeline.run` — run on a schedule (cron on a VPS, or an Azure Container
  Apps job on a timer). Needs `HUBSPOT_TOKEN` and Claude CLI auth
  (`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, i.e. the subscription).
- `uvicorn pipeline.publish.api:app` — the API, always on.
- `data/` on a mounted volume (Azure Files) so runs survive restarts.

Secrets live in the host's secret store (Azure Key Vault / Container Apps secrets),
never in the repo.

## 5. Data handling (needs review)

- HubSpot contact records are personal data. The pipeline drops PII in stage 2, before
  anything reaches Claude or the API. Raw pulls (which do contain PII) stay on the
  pipeline host and should get a retention limit.
- Deal amounts are financial data. The dashboard should be internal-only. Nothing from
  this pipeline should go into member- or customer-facing material without review.
- If HubSpot holds any circuit details, service locations, or call records, those are
  CPNI-adjacent and should be excluded at extract time. **Please confirm with
  compliance which HubSpot properties are in scope before the first real pull.**

## 6. Open questions

1. Which HubSpot hub/tier do we have? The campaigns and web-analytics APIs differ by tier
   (Marketing Hub Professional/Enterprise for campaign attribution). This decides what
   the extract stage can pull.
2. What counts as "revenue" — deal `amount` on closed-won, or something else (MRR,
   contract value)? Which deal pipelines are in scope?
3. How far back should the first backfill go?
4. Run frequency — daily is the default assumption.
5. Who uses the dashboard, and do they already have a BI tool?
6. VPS or Azure? (Azure fits better if the company is already on Microsoft 365/Entra.)
7. Claude stages run on Claude Code CLI with subscription auth (decided). Watch usage
   limits as run frequency grows; the runner is isolated in one file if we ever move
   to the API.
