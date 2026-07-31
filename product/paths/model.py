"""Immutable path values assembled by Product composition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    user_config_root: Path
    workspace_root: Path
    session_workspace_root: Path
    browser_profiles_root: Path
    sandbox_ca_root: Path
    secrets_root: Path
    oauth_root: Path
    package_data_root: Path
    codemap_root: Path
    logs_root: Path
    file_locks_root: Path
    service_journal_root: Path
    model_journal_root: Path


__all__ = ["RuntimePaths"]
