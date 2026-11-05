"""Pipeline orchestrator.

    python -m pipeline.run --source fixtures            # synthetic data, full pipeline
    python -m pipeline.run --source hubspot             # live pull (needs HUBSPOT_TOKEN)
    python -m pipeline.run --source fixtures --skip-claude

Writes everything for a run to data/runs/<run_id>/:
    raw/*.json        exactly what was pulled
    warehouse.sqlite  normalized, PII-free tables
    metrics.json      deterministic metrics
    agent_pack.md     what Claude reads
    analyst.json      Claude analyst output (draft findings)
    checks.json       code verification results
    review.json       Claude reviewer output
    findings.json     final findings with status (verified / revised / rejected)
    manifest.json     run summary
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from pipeline.analyze import claude_runner
from pipeline.analyze.schemas import AnalystOutput, ReviewerOutput
from pipeline.config import Config
from pipeline.metrics.compute import compute
from pipeline.metrics.service import compute_service
from pipeline.transform import agent_pack
from pipeline.transform.normalize import build_db
from pipeline.validate import validate
from pipeline.verify.checks import check_all

log = logging.getLogger("pipeline")
RUN_ID = re.compile(r"^\d{8}T\d{6}Z$")


def _write(path: Path, obj) -> None:
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj, indent=2, default=str))


def _previous_metrics(cfg: Config, current_run: str) -> dict[str, float] | None:
    if not cfg.runs_dir.exists():
        return None
    prior = sorted(p for p in cfg.runs_dir.iterdir()
                   if RUN_ID.match(p.name) and p.name < current_run and (p / "metrics.json").exists())
    if not prior:
        return None
    return {m["id"]: m["value"] for m in json.loads((prior[-1] / "metrics.json").read_text())}


def _context(cfg: Config) -> str:
    return Path(cfg.analysis_context).read_text() if cfg.analysis_context else ""


def extract(source: str, cfg: Config, csv_dir: str | None = None, raw_file: str | None = None) -> dict:
    if source == "fixtures":
        from pipeline.extract.fixtures import generate, generate_salesforce
        raw = generate()
        raw.update(generate_salesforce(raw))
        return raw
    if source == "raw":
        if not raw_file:
            sys.exit("--raw-file is required with --source raw")
        return json.loads(Path(raw_file).read_text())
    if source == "csv":
        if not csv_dir:
            sys.exit("--csv-dir is required with --source csv")
        from pipeline.extract.csv_import import load_dir
        return load_dir(Path(csv_dir), cfg.hash_salt)
    if source == "hubspot":
        if not cfg.hubspot_token:
            sys.exit("HUBSPOT_TOKEN is not set")
        from pipeline.extract.hubspot import pull
        raw = pull(cfg.hubspot_token, cfg.hash_salt)
        if cfg.salesforce_configured:
            from pipeline.extract import salesforce
            sf = salesforce.pull(cfg.sf_login_url, cfg.sf_client_id, cfg.sf_client_secret, cfg.hash_salt,
                                 cfg.sf_api_version)
            raw["_warnings"] = raw.get("_warnings", []) + sf.pop("_sf_warnings", [])
            raw.update(sf)
        else:
            log.info("Salesforce not configured; skipping service deals")
        return raw
    sys.exit(f"unknown source: {source}")


def analyze_and_verify(cfg: Config, run_dir: Path, pack: str, metrics) -> dict:
    analyst = claude_runner.run(
        cfg, claude_runner.load_prompt("analyst", min_sample=cfg.min_sample), pack, AnalystOutput,
        log_path=run_dir / "analyst_raw")
    _write(run_dir / "analyst.json", analyst.model_dump())

    checks = check_all(analyst.findings, metrics, cfg.min_sample, cfg.match_tolerance)
    _write(run_dir / "checks.json", [c.__dict__ for c in checks])
    passed_ids = {c.finding_id for c in checks if c.passed}
    survivors = [f for f in analyst.findings if f.finding_id in passed_ids]

    reviews = {}
    if survivors:
        # Reviewer sees the full metrics table, not just what was cited, so it can catch
        # cherry-picking and "highest of all" claims the cited evidence can't support.
        review_input = json.dumps({"findings": [f.model_dump() for f in survivors],
                                   "metrics": [m.to_dict() for m in metrics]}, indent=2)
        reviewer = claude_runner.run(cfg, claude_runner.load_prompt("reviewer"), review_input, ReviewerOutput,
                                     log_path=run_dir / "review_raw")
        _write(run_dir / "review.json", reviewer.model_dump())
        reviews = {r.finding_id: r for r in reviewer.reviews}

    check_by_id = {c.finding_id: c for c in checks}
    final = []
    for f in analyst.findings:
        c, r = check_by_id[f.finding_id], reviews.get(f.finding_id)
        if not c.passed:
            status, reason = "rejected", "; ".join(c.errors)
        elif r is None:
            status, reason = "unreviewed", "reviewer returned no verdict"
        elif r.verdict == "confirmed":
            status, reason = "verified", r.reason
        elif r.verdict == "revise":
            status, reason = "revised", r.reason
        else:
            status, reason = "rejected", r.reason
        row = f.model_dump() | {"status": status, "status_reason": reason, "check_warnings": c.warnings}
        if r and r.verdict == "revise" and r.revised_claim:
            row["original_claim"], row["claim"] = f.claim, r.revised_claim
        final.append(row)
    return {"summary": analyst.summary, "findings": final}


def main(argv: list[str] | None = None) -> Path:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["fixtures", "hubspot", "csv", "raw"], default="fixtures")
    ap.add_argument("--raw-file", help="Raw JSON in the extractor's shape (with --source raw)")
    ap.add_argument("--csv-dir", help="Folder of HubSpot CSV exports (with --source csv)")
    ap.add_argument("--skip-claude", action="store_true", help="Compute metrics only")
    ap.add_argument("--force", action="store_true", help="Run analysis even if validation finds errors")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    lock = open(cfg.data_dir / ".run.lock", "w")  # noqa: SIM115 - held for the whole run
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Another pipeline run is in progress; exiting.")

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    while (cfg.runs_dir / run_id).exists() or (cfg.runs_dir / f".{run_id}.partial").exists():
        time.sleep(1)  # back-to-back runs within one second
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    final_dir = cfg.runs_dir / run_id
    # Build in a hidden folder and publish with an atomic rename, so the dashboard
    # (possibly on another machine sharing the volume) never sees a half-written run.
    run_dir = cfg.runs_dir / f".{run_id}.partial"
    (run_dir / "raw").mkdir(parents=True)
    manifest = {"run_id": run_id, "source": args.source, "started_at": datetime.now(UTC).isoformat()}

    log.info("extract (%s)", args.source)
    raw = extract(args.source, cfg, args.csv_dir, args.raw_file)
    for name, rows in raw.items():
        _write(run_dir / "raw" / f"{name}.json", rows)
    manifest["record_counts"] = {k: len(v) for k, v in raw.items() if not k.startswith("_")}
    manifest["excluded_contact_ids"] = len(cfg.exclude_contact_ids)
    manifest["extract_warnings"] = raw.get("_warnings", [])

    log.info("organize")
    con = build_db(raw, run_dir / "warehouse.sqlite", cfg.exclude_contact_ids)
    if raw.get("sf_opportunities") is not None:
        from pipeline.transform.salesforce import build_sf
        manifest["salesforce_match"] = build_sf(con, raw)

    log.info("validate")
    report = validate(con, raw)
    _write(run_dir / "validation.json", report)
    manifest["validation"] = report["status"]
    for c in report["checks"]:
        if c["level"] != "info":
            log.warning("validation %s: %s", c["level"], c["message"])

    log.info("metrics")
    metrics = compute(con, cfg.converted_stages) + compute_service(con)
    _write(run_dir / "metrics.json", agent_pack.metrics_json(metrics))
    pack = agent_pack.build(con, metrics, _previous_metrics(cfg, run_id), context=_context(cfg),
                            notes=raw.get("_warnings", []))
    _write(run_dir / "agent_pack.md", pack)
    manifest["metric_count"] = len(metrics)

    if args.skip_claude:
        manifest["analysis"] = "skipped (--skip-claude)"
    elif report["status"] == "error" and not args.force:
        manifest["analysis"] = "skipped (validation errors; fix them or rerun with --force)"
        log.error(manifest["analysis"])
    elif not claude_runner.available(cfg):
        manifest["analysis"] = f"skipped ('{cfg.claude_bin}' not on PATH)"
        log.warning(manifest["analysis"])
    else:
        log.info("analyze + verify")
        try:
            result = analyze_and_verify(cfg, run_dir, pack, metrics)
            _write(run_dir / "findings.json", result)
            counts: dict[str, int] = {}
            for f in result["findings"]:
                counts[f["status"]] = counts.get(f["status"], 0) + 1
            manifest["analysis"] = "complete"
            manifest["finding_counts"] = counts
        except Exception as e:  # metrics are still published if Claude fails
            log.exception("analysis failed")
            manifest["analysis"] = f"failed: {e}"

    manifest["finished_at"] = datetime.now(UTC).isoformat()
    _write(run_dir / "manifest.json", manifest)
    con.close()
    run_dir.rename(final_dir)
    log.info("done: %s", final_dir)
    return final_dir


if __name__ == "__main__":
    main()
