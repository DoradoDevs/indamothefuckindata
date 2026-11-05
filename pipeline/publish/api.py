"""Read-only API the dashboard consumes.

    uvicorn pipeline.publish.api:app --port 8000

Serves the dashboard at / and the JSON API under /runs. If API_TOKEN is set, every
API route except /health requires `Authorization: Bearer <token>`. (In Azure, put
Entra ID sign-in in front of the whole app instead.)
"""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from pipeline.config import Config
from pipeline.publish import explore

cfg = Config()
app = FastAPI(title="HubSpot Analytics API", version="0.1.0")
_RUN_ID = re.compile(r"^\d{8}T\d{6}Z$")


def auth(authorization: str | None = Header(default=None)) -> None:
    if not cfg.api_token:
        return
    expected = f"Bearer {cfg.api_token}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(401, "unauthorized")


def _runs() -> list[Path]:
    if not cfg.runs_dir.exists():
        return []
    return sorted((p for p in cfg.runs_dir.iterdir() if (p / "manifest.json").exists()), reverse=True)


def _run_dir(run_id: str) -> Path:
    if run_id == "latest":
        # Newest run whose data passed validation; a broken pull never replaces a good dashboard.
        runs = _runs()
        if not runs:
            raise HTTPException(404, "no runs yet")
        for r in runs:
            if (_load(r / "manifest.json") or {}).get("validation") != "error":
                return r
        return runs[0]
    if not _RUN_ID.match(run_id):
        raise HTTPException(400, "bad run id")
    p = cfg.runs_dir / run_id
    if not (p / "manifest.json").exists():
        raise HTTPException(404, "run not found")
    return p


def _load(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/runs", dependencies=[Depends(auth)])
def list_runs(limit: int = Query(20, le=200)):
    return [_load(p / "manifest.json") for p in _runs()[:limit]]


@app.get("/runs/{run_id}", dependencies=[Depends(auth)])
def get_run(run_id: str):
    return _load(_run_dir(run_id) / "manifest.json")


@app.get("/runs/{run_id}/metrics", dependencies=[Depends(auth)])
def get_metrics(run_id: str, prefix: str | None = None):
    metrics = _load(_run_dir(run_id) / "metrics.json") or []
    return [m for m in metrics if not prefix or m["id"].startswith(prefix)]


@app.get("/runs/{run_id}/findings", dependencies=[Depends(auth)])
def get_findings(run_id: str, status: str = Query("published", pattern="^(published|verified|revised|rejected|unreviewed|all)$")):
    data = _load(_run_dir(run_id) / "findings.json")
    if data is None:
        return {"summary": None, "findings": []}
    keep = {"published": {"verified", "revised"}}.get(status, {status})
    findings = data["findings"] if status == "all" else [f for f in data["findings"] if f["status"] in keep]
    return {"summary": data.get("summary"), "findings": findings}


@app.get("/runs/{run_id}/validation", dependencies=[Depends(auth)])
def get_validation(run_id: str):
    return _load(_run_dir(run_id) / "validation.json") or {"status": "unknown", "checks": []}


# --- Dashboard queries ------------------------------------------------------

_MONTH = "^\\d{4}-\\d{2}$"
_COHORT_PREFIXES = ("campaign.", "source.", "industry.", "test.")


def _warehouse(run_id: str) -> Path:
    p = _run_dir(run_id) / "warehouse.sqlite"
    if not p.exists():
        raise HTTPException(404, "run has no warehouse")
    return p


@app.get("/runs/{run_id}/explore/options", dependencies=[Depends(auth)])
def explore_options(run_id: str):
    return explore.options(_warehouse(run_id))


@app.get("/runs/{run_id}/explore/summary", dependencies=[Depends(auth)])
def explore_summary(run_id: str, start: str = Query(..., pattern=_MONTH), end: str = Query(..., pattern=_MONTH),
                    compare: bool = True):
    if start > end:
        raise HTTPException(400, "start after end")
    wh = _warehouse(run_id)
    out = {"start": start, "end": end,
           "activity": explore.activity(wh, start, end),
           "cohort": explore.cohort_metrics(wh, start, end, _COHORT_PREFIXES),
           "service": explore.service_metrics(wh, start, end)}
    if compare:
        ps, pe = explore.previous_window(start, end)
        out["previous"] = {"start": ps, "end": pe, "activity": explore.activity(wh, ps, pe),
                           "cohort": explore.cohort_metrics(wh, ps, pe, _COHORT_PREFIXES),
                           "service": explore.service_metrics(wh, ps, pe)}
    return out


@app.get("/runs/{run_id}/explore/series", dependencies=[Depends(auth)])
def explore_series(run_id: str, measure: str, by: str = "none",
                   start: str = Query(..., pattern=_MONTH), end: str = Query(..., pattern=_MONTH)):
    try:
        return explore.series(_warehouse(run_id), measure, by, start, end)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# Dashboard (static files). Mounted last so API routes take precedence.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="dashboard")
