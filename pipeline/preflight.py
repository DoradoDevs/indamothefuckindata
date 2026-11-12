"""Preflight: is everything in place for a real run? Run this the day access lands.

    python -m pipeline.preflight            # settings only, no network
    python -m pipeline.preflight --live     # also calls HubSpot / Salesforce read-only

Prints a checklist with what's missing and how to fix it. Exit code 1 if any
required check fails.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

from pipeline.config import Config

OK, WARN, FAIL = "✓", "!", "✗"


def _hubspot_probe(token: str) -> list[tuple[str, str, str]]:
    """One tiny read per object type, to confirm the token's scopes."""
    probes = [
        ("contacts", "/crm/v3/objects/contacts?limit=1", True),
        ("companies", "/crm/v3/objects/companies?limit=1", True),
        ("deals", "/crm/v3/objects/deals?limit=1", True),
        ("deal pipelines", "/crm/v3/pipelines/deals", True),
        ("campaigns", "/marketing/v3/campaigns?limit=1", False),
        ("marketing emails", "/marketing/v3/emails?limit=1", False),
    ]
    out = []
    for name, path, required in probes:
        req = urllib.request.Request("https://api.hubapi.com" + path, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                total = json.loads(resp.read()).get("total")
            out.append((OK, f"HubSpot {name}", "readable" + (f" ({total:,} records)" if isinstance(total, int) else "")))
        except urllib.error.HTTPError as e:
            hint = {401: "token rejected; check HUBSPOT_TOKEN",
                    403: "token lacks this scope; ask the HubSpot admin to add it"}.get(e.code, f"HTTP {e.code}")
            out.append((FAIL if required else WARN, f"HubSpot {name}", hint))
        except urllib.error.URLError as e:
            out.append((FAIL, f"HubSpot {name}", f"network error: {e.reason}"))
    return out


def _salesforce_probe(cfg: Config) -> list[tuple[str, str, str]]:
    from pipeline.extract.salesforce import SalesforceClient, SalesforceError
    client = SalesforceClient(cfg.sf_login_url, cfg.sf_client_id, cfg.sf_client_secret, cfg.sf_api_version)
    try:
        client.authenticate()
    except SalesforceError as e:
        return [(FAIL, "Salesforce login", str(e))]
    out = [(OK, "Salesforce login", f"connected to {client.instance_url}")]
    for obj in ("Opportunity", "OpportunityContactRole", "Account", "Contact"):
        try:
            n = client.query_all(f"SELECT COUNT() FROM {obj}")
            out.append((OK, f"Salesforce {obj}", "readable" + (f" ({len(n)})" if n else "")))
        except urllib.error.HTTPError as e:
            out.append((FAIL if obj == "Opportunity" else WARN, f"Salesforce {obj}",
                        f"HTTP {e.code}; the integration user needs read access"))
    return out


def run_checks(cfg: Config, live: bool = False) -> list[tuple[str, str, str]]:
    r: list[tuple[str, str, str]] = []
    r.append((OK, "HubSpot token", "set") if cfg.hubspot_token else
             (FAIL, "HubSpot token", "HUBSPOT_TOKEN not set; needs the admin-issued read-only private app token"))
    if not cfg.hash_salt:
        r.append((FAIL, "Hash key", "HASH_SALT not set; generate one with: python -c \"import secrets; "
                  "print(secrets.token_hex(32))\" and store it in the secret store"))
    elif len(cfg.hash_salt) < 32:
        r.append((WARN, "Hash key", "HASH_SALT is short; use at least 32 random characters"))
    else:
        r.append((OK, "Hash key", "set"))
    sf = [cfg.sf_login_url, cfg.sf_client_id, cfg.sf_client_secret]
    if all(sf):
        r.append((OK, "Salesforce settings", "set"))
    elif any(sf):
        r.append((FAIL, "Salesforce settings", "partly set; need SF_LOGIN_URL, SF_CLIENT_ID, SF_CLIENT_SECRET"))
    else:
        r.append((WARN, "Salesforce settings", "not set; service-deal analysis will be skipped"))

    if shutil.which(cfg.claude_bin):
        try:
            v = subprocess.run([cfg.claude_bin, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
            r.append((OK, "Claude CLI", v or "installed"))
        except (OSError, subprocess.SubprocessError) as e:
            r.append((FAIL, "Claude CLI", f"installed but won't run: {e}"))
    else:
        r.append((WARN, "Claude CLI", "not found; runs will publish metrics without findings"))
    if os.environ.get("ANTHROPIC_API_KEY"):
        r.append((WARN, "Claude billing", "ANTHROPIC_API_KEY is set, so the CLI bills the API, not the subscription"))
    elif os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        r.append((OK, "Claude billing", "subscription token set"))
    else:
        r.append((WARN, "Claude billing", "CLAUDE_CODE_OAUTH_TOKEN not set (fine if the CLI is logged in locally)"))

    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.data_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        r.append((OK, "Data folder", f"{cfg.data_dir.resolve()} is writable"))
    except OSError as e:
        r.append((FAIL, "Data folder", f"{cfg.data_dir} not writable: {e}"))
    r.append((OK, "Dashboard access", "API_TOKEN set") if cfg.api_token else
             (WARN, "Dashboard access", "API_TOKEN not set; fine behind load-balancer sign-in, otherwise the API is open"))
    if not cfg.exclude_contact_ids:
        r.append((WARN, "Sample contacts", "EXCLUDE_CONTACT_IDS not set; HubSpot's 2 sample contacts will be counted"))

    if live:
        if cfg.hubspot_token:
            r += _hubspot_probe(cfg.hubspot_token)
        if all(sf):
            r += _salesforce_probe(cfg)
    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="Also make read-only test calls to HubSpot/Salesforce")
    args = ap.parse_args(argv)
    results = run_checks(Config(), args.live)
    width = max(len(name) for _, name, _ in results)
    for mark, name, detail in results:
        print(f" {mark}  {name.ljust(width)}  {detail}")
    failed = sum(1 for mark, _, _ in results if mark == FAIL)
    print(f"\n{'Ready.' if not failed else f'{failed} required item(s) missing.'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
