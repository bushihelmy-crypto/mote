"""File-watch config — deploy-time, pure-data settings (like ``lsp_config.py``).

Lives in ``common/schema`` alongside the other declarative role policies so
``RoleSchema`` (which declares it) can reference it without importing the watch
service. The service itself lives in ``metagpt.environment.watching``; this is
only the declarative shape: which roots to watch and how often to poll.

Backward compatibility: a Role with ``file_watch=None`` (the default) runs with
no watcher. ``FileWatchConfig.enabled`` is an explicit master switch so the
subsystem can be toggled without dropping config.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: Default ignore globs — VCS metadata, caches, and the agent's own workspace
#: scratch dirs (session logs / blobs / residency), so they never echo back.
_DEFAULT_IGNORE = [
    ".git",
    ".hg",
    ".svn",
    "*.pyc",
    "__pycache__",
    ".agent_sessions",
    ".agent_residency",
]


class FileWatchConfig(BaseModel):
    """Per-Role file-watch policy, declared on :class:`RoleSchema`.

    ``roots`` are the directories/files to watch; empty means "default to the
    Role's project root (or cwd)". ``ignore`` is a list of ``fnmatch`` globs
    tested against the full path and each path component. ``check_interval`` is
    the poll period in seconds. ``enabled`` is the master switch.
    """

    enabled: bool = Field(default=False, description="Master switch for the file-watch subsystem.")
    roots: list[str] = Field(
        default_factory=list,
        description="Directories/files to watch. Empty => default to the project root (or cwd).",
    )
    ignore: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_IGNORE),
        description="fnmatch globs to ignore (matched against full path and each path component).",
    )
    check_interval: float = Field(
        default=1.0,
        description="Polling period in seconds.",
    )
    reload_skills: bool = Field(
        default=False,
        description=(
            "Hot-reload skills when a SKILL.md changes. Auto-registers a "
            "FileChanged handler and extends the watched roots to the builtin "
            "skill directory."
        ),
    )
    reload_config: bool = Field(
        default=False,
        description=(
            "Hot-reload the layered config when a config.yaml source file "
            "changes. Auto-registers a FileChanged handler and extends the "
            "watched roots to the discovered config source files. Note: only "
            "components built after the reload observe the new config; already "
            "built collaborators keep their snapshot for the session."
        ),
    )


__all__ = ["FileWatchConfig"]
