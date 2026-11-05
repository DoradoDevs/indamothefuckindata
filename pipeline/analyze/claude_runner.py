"""Runs Claude Code CLI in headless mode (`claude -p`) with structured output.

Kept in one file so we can swap to the Claude API (better cost caps and monitoring in
production) without touching the rest of the pipeline.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from pipeline.config import Config

PROMPTS = Path(__file__).parent / "prompts"
T = TypeVar("T", bound=BaseModel)


class ClaudeUnavailable(RuntimeError):
    pass


def available(cfg: Config) -> bool:
    return shutil.which(cfg.claude_bin) is not None


def load_prompt(name: str, **fmt) -> str:
    text = (PROMPTS / f"{name}.md").read_text()
    for k, v in fmt.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def _extract_payload(stdout: str) -> dict:
    envelope = json.loads(stdout)
    if envelope.get("is_error"):
        raise RuntimeError(f"claude returned an error: {envelope.get('result')}")
    if isinstance(envelope.get("structured_output"), dict):
        return envelope["structured_output"]
    result = envelope.get("result", "")
    # Fallback: model put JSON in the text result (strip code fences if present).
    result = result.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(result)


def run(cfg: Config, system_prompt: str, user_input: str, schema: type[T], log_path: Path | None = None,
        retries: int = 1) -> T:
    if not available(cfg):
        raise ClaudeUnavailable(f"'{cfg.claude_bin}' not found on PATH")
    cmd = [
        cfg.claude_bin, "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(schema.model_json_schema()),
        "--system-prompt", system_prompt,
        "--tools", "",  # analysis only: no file, shell, or web access
        "--no-session-persistence",
    ]
    if cfg.claude_model:
        cmd += ["--model", cfg.claude_model]

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        proc = subprocess.run(cmd, input=user_input, capture_output=True, text=True, timeout=cfg.claude_timeout_s)
        if log_path:
            log_path.with_suffix(f".attempt{attempt}.json").write_text(proc.stdout or proc.stderr)
        if proc.returncode != 0:
            last_err = RuntimeError(f"claude exited {proc.returncode}: {proc.stderr[-2000:]}")
            continue
        try:
            return schema.model_validate(_extract_payload(proc.stdout))
        except (json.JSONDecodeError, ValidationError, RuntimeError) as e:
            last_err = e
    raise RuntimeError(f"Claude output failed after {retries + 1} attempt(s): {last_err}")
