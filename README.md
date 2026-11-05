# HubSpot Analytics Pipeline (DRAFT)

HubSpot data → deterministic metrics → Claude analysis → verification (code + a second
Claude pass) → read-only API for a dashboard. See [`docs/DESIGN.md`](docs/DESIGN.md) for
the architecture, data-handling notes, and open questions.

## Quick start

```bash
pip install -e ".[dev]"

python -m pytest -q                              # tests
python -m pipeline.run --skip-claude             # synthetic data, metrics only
python -m pipeline.run                           # synthetic data + Claude analysis/verify
uvicorn pipeline.publish.api:app --port 8000     # serve results
```

Live HubSpot (once access is approved): `HUBSPOT_TOKEN=... python -m pipeline.run --source hubspot`.
See `.env.example` for all settings.

## Claude auth (subscription, no API key)

The Claude stages run through Claude Code CLI, which can use a Claude subscription:

1. On any machine logged in to the subscription, run `claude setup-token` and copy the token.
2. Set it as `CLAUDE_CODE_OAUTH_TOKEN` on the pipeline host (Azure: a Container Apps secret).
3. Make sure `ANTHROPIC_API_KEY` is **not** set. If it is, the CLI bills the API instead.

## Layout

| Path | Stage |
|---|---|
| `pipeline/extract/hubspot.py` | 1. Pull from HubSpot API (not yet tested against a live portal) |
| `pipeline/extract/fixtures.py` | 1. Synthetic data with planted patterns, for dev/testing |
| `pipeline/transform/normalize.py` | 2. Raw JSON → SQLite tables; drops PII via allow-list |
| `pipeline/metrics/compute.py` | 3. Deterministic metrics with stable IDs and sample sizes |
| `pipeline/transform/agent_pack.py` | 3. Compact metrics bundle Claude reads |
| `pipeline/analyze/` | 4a. `claude -p` analyst pass, JSON-schema output |
| `pipeline/verify/checks.py` | 4b. Every number Claude quotes must match a metric |
| `pipeline/analyze/prompts/reviewer.md` | 4c. Second Claude pass reviews the reasoning |
| `pipeline/publish/api.py` | 5. FastAPI for the dashboard |
| `Dockerfile` | One image: API by default, `python -m pipeline.run` as the scheduled job |

## Each run's output

`data/runs/<run_id>/` keeps raw pulls, the warehouse, metrics, the agent pack, both Claude
outputs, the check results, and the final `findings.json`. Every number on the dashboard
can be traced back to the raw records.

Finding statuses: `verified` (passed code check and reviewer), `revised` (reviewer
reworded it), `rejected` (failed code check or reviewer), `unreviewed`. The API returns
`verified` + `revised` by default.

## Results on synthetic data

On the synthetic dataset, a full run takes about 2 minutes and uses two Claude calls (about $0.45 at list
price; on a subscription that counts against usage limits instead of being billed). The pipeline found every planted pattern: the high-value
webinar campaign, the paid-search campaign with high volume and low conversion, the social
campaign with zero wins, the one-month lag from traffic to contacts, and the deals missing
amounts. The code check rejected a finding that used a ratio Claude worked out itself.

**Known gap:** the reviewer verified an industry win-rate difference that the synthetic
data doesn't plant on purpose. Next step: add confidence intervals and significance tests to
the ratio metrics, so Claude can tell which differences are real.
