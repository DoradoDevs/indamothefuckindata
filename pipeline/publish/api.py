"""Read-only API the dashboard consumes.

    uvicorn pipeline.publish.api:app --port 8000

If API_TOKEN is set, every route except /health requires `Authorization: Bearer <token>`.
"""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from pipeline.config import Config

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
        runs = _runs()
        if not runs:
            raise HTTPException(404, "no runs yet")
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
