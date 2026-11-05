"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "data")))
    hubspot_token: str | None = field(default_factory=lambda: os.environ.get("HUBSPOT_TOKEN"))
    claude_bin: str = field(default_factory=lambda: os.environ.get("CLAUDE_BIN", "claude"))
    claude_model: str | None = field(default_factory=lambda: os.environ.get("CLAUDE_MODEL"))
    claude_timeout_s: int = field(default_factory=lambda: int(os.environ.get("CLAUDE_TIMEOUT_S", "600")))
    # Findings citing metrics with n below this must carry a caveat.
    min_sample: int = field(default_factory=lambda: int(os.environ.get("MIN_SAMPLE", "20")))
    # Relative tolerance when matching a quoted number to a metric (covers rounding).
    match_tolerance: float = field(default_factory=lambda: float(os.environ.get("MATCH_TOLERANCE", "0.01")))
    api_token: str | None = field(default_factory=lambda: os.environ.get("API_TOKEN"))
    # Secret key for one-way hashing of contact emails (HubSpot <-> Salesforce matching).
    # Must be identical for both sources and kept in the secret store, never in the repo.
    hash_salt: str = field(default_factory=lambda: os.environ.get("HASH_SALT", ""))
    # Salesforce (optional; pulled alongside HubSpot when all three are set)
    sf_login_url: str = field(default_factory=lambda: os.environ.get("SF_LOGIN_URL", ""))
    sf_client_id: str = field(default_factory=lambda: os.environ.get("SF_CLIENT_ID", ""))
    sf_client_secret: str = field(default_factory=lambda: os.environ.get("SF_CLIENT_SECRET", ""))
    sf_api_version: str = field(default_factory=lambda: os.environ.get("SF_API_VERSION", "v61.0"))
    # Lifecycle stages that count as "converted" (reached the account's goal status).
    converted_stages: frozenset[str] = field(default_factory=lambda: frozenset(
        x.strip() for x in os.environ.get("CONVERTED_STAGES", "customer").split(",") if x.strip()))
    # Optional plain-text file describing the business context for the Claude passes.
    analysis_context: str = field(default_factory=lambda: os.environ.get("ANALYSIS_CONTEXT", ""))
    # Keep names, emails, and deal names for internal action lists (never sent to Claude).
    include_contact_details: bool = field(default_factory=lambda: os.environ.get(
        "INCLUDE_CONTACT_DETAILS", "false").lower() in ("1", "true", "yes"))
    # Email domains of the organization's own staff; those contacts are left out of the analysis.
    internal_domains: frozenset[str] = field(default_factory=lambda: frozenset(
        x.strip().lower() for x in os.environ.get("INTERNAL_DOMAINS", "").split(",") if x.strip()))
    # HubSpot portal ID, for links to records in the action lists.
    hubspot_portal_id: str = field(default_factory=lambda: os.environ.get("HUBSPOT_PORTAL_ID", ""))
    # Company types treated as key accounts in the action lists.
    key_segments: tuple[str, ...] = field(default_factory=lambda: tuple(
        x.strip() for x in os.environ.get("KEY_SEGMENTS", "Member,Customer").split(",") if x.strip()))
    # HubSpot record IDs to drop (e.g. the sample contacts HubSpot creates in new accounts).
    exclude_contact_ids: frozenset[str] = field(default_factory=lambda: frozenset(
        x.strip() for x in os.environ.get("EXCLUDE_CONTACT_IDS", "").split(",") if x.strip()))

    @property
    def salesforce_configured(self) -> bool:
        return bool(self.sf_login_url and self.sf_client_id and self.sf_client_secret)

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"
