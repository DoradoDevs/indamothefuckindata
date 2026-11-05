# Sales & Marketing Insights — HubSpot + Salesforce analytics pipeline (DRAFT)

> **Status:** built and tested end to end on sample data; **waiting on HubSpot and
> Salesforce access** for the first real run. See [`docs/STATUS.md`](docs/STATUS.md) for
> what's blocked, who it's waiting on, and the step-by-step for the day access lands.

HubSpot marketing data and Salesforce service deals → deterministic metrics with
confidence ranges and significance tests → Claude analysis → verification (code + an
independent Claude review) → read-only API → dashboard for INDATEL sales staff and
leadership. Internal and confidential.

| Doc | What's in it |
|---|---|
| [`HANDOFF.md`](HANDOFF.md) | **Start here.** Where we are, what exists, what's next |
| [`docs/STATUS.md`](docs/STATUS.md) | Where things stand, what we're waiting on, day-one checklist |
| [`docs/DESIGN.md`](docs/DESIGN.md) | Architecture, accuracy safeguards, data handling, what the live account looks like |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | Azure deployment (Container Apps, Key Vault, Entra ID sign-in) |

## Quick start

```bash
pip install -e ".[dev]"

python -m pytest -q                              # tests (39)
ruff check .                                     # lint
python -m pipeline.run --skip-claude             # sample data, metrics only
python -m pipeline.run                           # sample data + Claude analysis and review
uvicorn pipeline.publish.api:app --port 8000     # dashboard at http://localhost:8000
```

Real data:

```bash
python -m pipeline.preflight --live              # checklist: settings, secrets, token scopes
python -m pipeline.run --source hubspot          # HubSpot (+ Salesforce if SF_* are set)
python -m pipeline.run --source csv --csv-dir X  # HubSpot UI exports instead of the API
python -m pipeline.prune                         # data retention
```

See `.env.example` for all settings. All data in this repo is synthetic; real runs write
to `data/`, which is git-ignored.

## Claude auth (subscription, no API key)

1. On any machine logged in to the subscription, run `claude setup-token` and copy the token.
2. Set it as `CLAUDE_CODE_OAUTH_TOKEN` on the pipeline host (Azure: Key Vault).
3. Make sure `ANTHROPIC_API_KEY` is **not** set. If it is, the CLI bills the API instead.

## Layout

| Path | What it does |
|---|---|
| `pipeline/extract/hubspot.py` | HubSpot API pull (read-only token); email hashed in memory |
| `pipeline/extract/salesforce.py` | Salesforce pull (client credentials, read-only SOQL) |
| `pipeline/extract/csv_import.py` | HubSpot UI export files → same shape as the API |
| `pipeline/extract/fixtures.py` | Sample HubSpot + Salesforce data with planted patterns |
| `pipeline/transform/normalize.py` | Raw → SQLite tables; personal fields dropped by allow-list |
| `pipeline/transform/salesforce.py` | Salesforce tables; matching to HubSpot; marketing credit rule |
| `pipeline/validate.py` | Pull validation report (counts, fill rates, match rates) |
| `pipeline/metrics/compute.py` | Metrics with stable IDs, sample sizes, 95% ranges, group tests |
| `pipeline/metrics/service.py` | Marketing → service deal metrics |
| `pipeline/metrics/stats.py` | Wilson, Fisher exact, Welch, correlation ranges, Benjamini-Hochberg |
| `pipeline/transform/agent_pack.py` | Compact metrics bundle Claude reads |
| `pipeline/analyze/` | `claude -p` analyst and reviewer passes, JSON-schema output |
| `pipeline/verify/checks.py` | Every number Claude quotes must match a metric; comparison and significance rules |
| `pipeline/publish/api.py`, `explore.py` | FastAPI: JSON API, date-range queries, serves the dashboard |
| `pipeline/publish/static/` | Dashboard (plain HTML/JS, no build step) |
| `pipeline/preflight.py`, `pipeline/prune.py` | Day-one checklist; data retention |
| `Dockerfile`, `.github/workflows/ci.yml` | One image for app and jobs; CI (lint, tests, sample run) |

## Each run's output

`data/runs/<run_id>/` keeps raw pulls, the warehouse, validation report, metrics, the agent
pack, both Claude outputs, the check results, and the final `findings.json`. Runs are
built in a hidden folder and published by rename, and the dashboard shows the newest run
that passed validation.

Finding statuses: `verified` (passed the code check and the reviewer), `revised` (reviewer
reworded it), `rejected` (failed either), `unreviewed`. The dashboard shows `verified` +
`revised` by default, and the rest on **All findings** with the reason.

## Dashboard

Served by the API at `/`. One filter row (time period, compare with the period before)
scopes every number. Pages: Overview, Campaigns, Lead sources, Industries, Service deals
(when Salesforce data exists), What moves together, All findings, Data health, How to read
this. Bars show a likely range; tables have a plain-language "Stands out?" column. Every
chart has a table view; works on phones and in dark mode.
