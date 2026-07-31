"""Resource limits for durable File Operations data."""

from __future__ import annotations

from mote.runtime.artifacts.budgets import ARTIFACT_HARD_LIMIT_BYTES
from mote.runtime.fileops.metadata_manifest import MAX_METADATA_MANIFEST_BYTES

ARTIFACT_WRITE_TTL_SECONDS = 10 * 60.0
MAX_READ_MANIFEST_BYTES = 64 * 1_024
MAX_MATERIALIZED_TEXT_BYTES = 50 * 1_024 * 1_024
MAX_EDIT_PLAN_ARTIFACT_BYTES = 256 * 1_024 * 1_024
MAX_EDIT_PLAN_REVIEW_FACT_BYTES = MAX_MATERIALIZED_TEXT_BYTES
MAX_SEARCH_RESULT_BYTES = 256 * 1_024 * 1_024
MAX_SEARCH_MANIFEST_BYTES = 1 * 1_024 * 1_024


def snapshot_budget(source_bytes: int) -> int:
    if type(source_bytes) is not int or source_bytes < 0:
        raise ValueError("snapshot source size must be a non-negative integer")
    return source_bytes + MAX_METADATA_MANIFEST_BYTES


__all__ = [
    "ARTIFACT_HARD_LIMIT_BYTES",
    "ARTIFACT_WRITE_TTL_SECONDS",
    "MAX_EDIT_PLAN_ARTIFACT_BYTES",
    "MAX_EDIT_PLAN_REVIEW_FACT_BYTES",
    "MAX_MATERIALIZED_TEXT_BYTES",
    "MAX_READ_MANIFEST_BYTES",
    "MAX_SEARCH_MANIFEST_BYTES",
    "MAX_SEARCH_RESULT_BYTES",
    "snapshot_budget",
]
