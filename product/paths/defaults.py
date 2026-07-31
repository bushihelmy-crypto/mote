"""Default Product path construction."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import mote
from mote.product.paths.model import RuntimePaths

MOTE_PACKAGE_DIR = Path(mote.__file__).resolve().parent


def default_runtime_paths(
    *,
    user_config_root: Path | None = None,
    workspace_root: Path | None = None,
    **overrides: Path,
) -> RuntimePaths:
    user_root = user_config_root or Path.home() / ".mote"
    workspace = workspace_root or user_root / "workspace"
    paths = RuntimePaths(
        user_config_root=user_root,
        workspace_root=workspace,
        session_workspace_root=workspace,
        browser_profiles_root=user_root / "browser_profiles",
        sandbox_ca_root=user_root / "sandbox_ca",
        secrets_root=user_root,
        oauth_root=user_root / "oauth",
        package_data_root=MOTE_PACKAGE_DIR,
        codemap_root=user_root / "codemap",
        logs_root=user_root / "logs",
        file_locks_root=user_root / "locks",
        service_journal_root=user_root / "service_journal",
        model_journal_root=user_root / "model_journal",
    )
    return replace(paths, **overrides) if overrides else paths


__all__ = ["MOTE_PACKAGE_DIR", "default_runtime_paths"]
