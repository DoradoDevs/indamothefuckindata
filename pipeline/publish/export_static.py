"""Export the dashboard as one self-contained HTML file (data included), for sharing a
snapshot internally without a server.

    python -m pipeline.publish.export_static --out insights.html

Every API response the dashboard needs for the preset time periods is computed now and
embedded in the page. Custom month ranges aren't available in a snapshot. The file
contains confidential data (deal values): share it internally only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from fastapi.testclient import TestClient

from pipeline.publish import api

STATIC = Path(__file__).parent / "static"
PRESETS = ("3", "6", "12", "ytd", "all")
MEASURES = ("won_value", "deals_won", "new_contacts", "deals_created", "sessions")
GROUPINGS = ("none", "campaign", "source", "industry", "pipeline")


def _shift(month: str, n: int) -> str:
    y, m = int(month[:4]), int(month[5:7]) + n
    while m <= 0:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return f"{y:04d}-{m:02d}"


def _window(preset: str, months: list[str]) -> tuple[str, str]:
    """Same rules as computePeriod() in app.js."""
    first, last = months[0], months[-1]
    if preset == "all":
        start = first
    elif preset == "ytd":
        start = last[:4] + "-01"
    else:
        start = _shift(last, -(int(preset) - 1))
    return max(start, first), last


def collect(client: TestClient) -> dict[str, object]:
    data: dict[str, object] = {}

    def get(path: str):
        r = client.get(path)
        r.raise_for_status()
        data[path] = r.json()
        return data[path]

    for path in ("/runs/latest", "/runs/latest/metrics", "/runs/latest/findings?status=all", "/runs/latest/validation"):
        get(path)
    opts = get("/runs/latest/explore/options")
    windows = {_window(p, opts["months"]) for p in PRESETS}
    for start, end in sorted(windows):
        for compare in ("true", "false"):
            get(f"/runs/latest/explore/summary?start={start}&end={end}&compare={compare}")
        for measure in MEASURES:
            for by in GROUPINGS:
                get(f"/runs/latest/explore/series?measure={measure}&by={by}&start={start}&end={end}")
    return data


def build_html(data: dict[str, object]) -> str:
    html = (STATIC / "index.html").read_text()
    css = (STATIC / "styles.css").read_text()
    js = (STATIC / "app.js").read_text()
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace('<link rel="stylesheet" href="styles.css">', f"<style>\n{css}\n</style>")
    html = html.replace('<option value="custom">Choose months…</option>', "")
    html = html.replace('<script src="app.js"></script>',
                        f"<script>window.__STATIC__ = {payload};</script>\n<script>\n{js}\n</script>")
    banner = ('<div class="note" style="margin:0;border-radius:0">Snapshot file: data as of the date shown at the top. '
              'Preset time periods only. Internal and confidential: do not share outside the company.</div>')
    return html.replace('<nav class="tabs"', banner + '\n  <nav class="tabs"', 1)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="Output .html path")
    args = ap.parse_args(argv)
    if api.cfg.api_token:
        client = TestClient(api.app, headers={"Authorization": f"Bearer {api.cfg.api_token}"})
    else:
        client = TestClient(api.app)
    data = collect(client)
    Path(args.out).write_text(build_html(data))
    print(f"Wrote {args.out} ({len(data)} responses, {Path(args.out).stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
