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
    # HubSpot record IDs to drop (e.g. the sample contacts HubSpot creates in new accounts).
    exclude_contact_ids: frozenset[str] = field(default_factory=lambda: frozenset(
        x.strip() for x in os.environ.get("EXCLUDE_CONTACT_IDS", "").split(",") if x.strip()))

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"
