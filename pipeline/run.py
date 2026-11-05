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
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.analyze import claude_runner
from pipeline.analyze.schemas import AnalystOutput, ReviewerOutput
from pipeline.config import Config
from pipeline.metrics.compute import compute
from pipeline.transform import agent_pack
from pipeline.transform.normalize import build_db
from pipeline.verify.checks import check_all

log = logging.getLogger("pipeline")


def _write(path: Path, obj) -> None:
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj, indent=2, default=str))


def _previous_metrics(cfg: Config, current_run: str) -> dict[str, float] | None:
    if not cfg.runs_dir.exists():
        return None
    prior = sorted(p for p in cfg.runs_dir.iterdir() if p.name < current_run and (p / "metrics.json").exists())
    if not prior:
        return None
    return {m["id"]: m["value"] for m in json.loads((prior[-1] / "metrics.json").read_text())}


def extract(source: str, cfg: Config) -> dict:
    if source == "fixtures":
        from pipeline.extract.fixtures import generate
        return generate()
    if source == "hubspot":
        if not cfg.hubspot_token:
            sys.exit("HUBSPOT_TOKEN is not set")
        from pipeline.extract.hubspot import pull
        return pull(cfg.hubspot_token)
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
    ap.add_argument("--source", choices=["fixtures", "hubspot"], default="fixtures")
    ap.add_argument("--skip-claude", action="store_true", help="Compute metrics only")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = cfg.runs_dir / run_id
    (run_dir / "raw").mkdir(parents=True)
    manifest = {"run_id": run_id, "source": args.source, "started_at": datetime.now(timezone.utc).isoformat()}

    log.info("extract (%s)", args.source)
    raw = extract(args.source, cfg)
    for name, rows in raw.items():
        _write(run_dir / "raw" / f"{name}.json", rows)
    manifest["record_counts"] = {k: len(v) for k, v in raw.items() if not k.startswith("_")}
    manifest["extract_warnings"] = raw.get("_warnings", [])

    log.info("organize")
    con = build_db(raw, run_dir / "warehouse.sqlite")

    log.info("metrics")
    metrics = compute(con)
    _write(run_dir / "metrics.json", agent_pack.metrics_json(metrics))
    pack = agent_pack.build(con, metrics, _previous_metrics(cfg, run_id))
    _write(run_dir / "agent_pack.md", pack)
    manifest["metric_count"] = len(metrics)

    if args.skip_claude:
        manifest["analysis"] = "skipped (--skip-claude)"
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

    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write(run_dir / "manifest.json", manifest)
    log.info("done: %s", run_dir)
    return run_dir


if __name__ == "__main__":
    main()
