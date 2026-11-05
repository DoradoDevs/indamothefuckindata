"""Data retention: delete old runs, and raw pulls sooner than the rest.

    python -m pipeline.prune                       # defaults below
    python -m pipeline.prune --keep-days 180 --raw-days 30 --keep-min 5 --dry-run

Raw pulls hold the most detail (company names, hashed emails), so they're removed
first; a run's metrics and findings stay longer for trend history. The newest
--keep-min runs are always kept whole.
"""
from __future__ import annotations

import argparse
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pipeline.config import Config


def _run_time(p: Path) -> datetime | None:
    try:
        return datetime.strptime(p.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def prune(runs_dir: Path, keep_days: int, raw_days: int, keep_min: int, dry_run: bool = False,
          now: datetime | None = None) -> dict[str, list[str]]:
    now = now or datetime.now(UTC)
    runs = sorted((p for p in runs_dir.iterdir() if p.is_dir() and _run_time(p)), reverse=True) \
        if runs_dir.exists() else []
    removed, raw_removed = [], []
    for i, run in enumerate(runs):
        if i < keep_min:
            continue
        age = now - _run_time(run)
        if age > timedelta(days=keep_days):
            removed.append(run.name)
            if not dry_run:
                shutil.rmtree(run)
        elif age > timedelta(days=raw_days):
            for target in (run / "raw", run / "warehouse.sqlite"):
                if target.exists():
                    raw_removed.append(f"{run.name}/{target.name}")
                    if not dry_run:
                        shutil.rmtree(target) if target.is_dir() else target.unlink()
    return {"runs_removed": removed, "raw_removed": raw_removed}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep-days", type=int, default=180, help="Delete whole runs older than this")
    ap.add_argument("--raw-days", type=int, default=30, help="Delete raw pulls and warehouses older than this")
    ap.add_argument("--keep-min", type=int, default=5, help="Always keep this many newest runs whole")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    result = prune(Config().runs_dir, args.keep_days, args.raw_days, args.keep_min, args.dry_run)
    verb = "Would remove" if args.dry_run else "Removed"
    print(f"{verb} {len(result['runs_removed'])} run(s) and {len(result['raw_removed'])} raw item(s).")
    for x in result["runs_removed"] + result["raw_removed"]:
        print("  ", x)


if __name__ == "__main__":
    main()
