"""Activation barrier for Sessions that still contain a legacy run journal."""

from __future__ import annotations

import json
from pathlib import Path

RUN_DOMAIN_MANIFEST_SCHEMA = "mote.run-domain-cutover/v1"
RUN_DOMAIN_MANIFEST_FILE = "run-domain-manifest.json"
LEGACY_RUN_JOURNAL_FILE = "run-journal.jsonl"


def require_run_domain_activation(ledger_directory: Path) -> None:
    legacy = ledger_directory / LEGACY_RUN_JOURNAL_FILE
    manifest = ledger_directory / RUN_DOMAIN_MANIFEST_FILE
    if not legacy.exists():
        return
    try:
        raw = json.loads(manifest.read_bytes())
    except FileNotFoundError as exc:
        raise RuntimeError("legacy run journal requires completed domain cutover") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("run-domain cutover manifest is corrupt") from exc
    fields = {"schema", "session_id", "source_digest", "candidate_digests", "retention_until"}
    if (
        type(raw) is not dict
        or set(raw) != fields
        or raw.get("schema") != RUN_DOMAIN_MANIFEST_SCHEMA
        or type(raw["session_id"]) is not str
        or type(raw["source_digest"]) is not str
        or type(raw["candidate_digests"]) is not dict
        or set(raw["candidate_digests"]) != {"tool", "model", "timer"}
        or any(type(value) is not str for value in raw["candidate_digests"].values())
        or type(raw["retention_until"]) is not str
    ):
        raise RuntimeError("run-domain cutover manifest is invalid")


__all__ = [
    "LEGACY_RUN_JOURNAL_FILE",
    "RUN_DOMAIN_MANIFEST_FILE",
    "RUN_DOMAIN_MANIFEST_SCHEMA",
    "require_run_domain_activation",
]
