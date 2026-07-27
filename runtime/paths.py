#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Runtime path anchors and layered ``.mote`` discovery."""
import json
from pathlib import Path
from typing import List, Optional

from loguru import logger

import mote
from mote.runtime.vcs.probe import find_git_root

#: The per-project config directory name. All project-local mote assets
#: (skills / agents / mcp.json / settings.local.json) live under ``<dir>/.mote``.
MOTE_DIR_NAME = ".mote"

#: The installed ``mote`` package directory — the single deterministic anchor for
#: assets SHIPPED INSIDE the package (``config.example.yaml``, prompt templates,
#: bundled schemas, …). Resolved straight from the import system
#: (``mote.__file__``), so it is correct under editable installs, wheels, zipapps
#: and containers alike — no ``cwd`` guessing, no ``.git`` marker heuristics, and
#: no env override needed. A caller that wants the *containing* directory can use
#: the explicit :pyattr:`MOTE_PACKAGE_DIR.parent`.
MOTE_PACKAGE_DIR = Path(mote.__file__).resolve().parent


# MOTE ROOTS — user config home + per-session workspace. These are anchored at
# the user's home directory, independent of where mote is installed or launched.
CONFIG_ROOT = Path.home() / ".mote"
DEFAULT_WORKSPACE_ROOT = CONFIG_ROOT / "workspace"


# Storage root for serialized documents (schema/document.py) — TODO: store
# `storage` under the individual generated project.
SERDESER_PATH = DEFAULT_WORKSPACE_ROOT / "storage"


# Durable browser-login store (under the config home): each named profile is an
# ENCRYPTED Playwright ``storage_state`` (cookies + localStorage), reusing the
# vault key. Anchored at the config home (not a session workspace) because a
# login identity outlives any single session. Single source of truth for both
# the profile store and the sandbox mask that hides it from confined commands.
BROWSER_PROFILES_DIRNAME = "browser_profiles"


def browser_profiles_dir() -> Path:
    """The ``~/.mote/browser_profiles`` directory (durable browser-login store)."""
    return CONFIG_ROOT / BROWSER_PROFILES_DIRNAME


# ============================================================================
# Workspace layout — the per-session artifact tree.
# ----------------------------------------------------------------------------
# Every artifact a session produces lives UNDER one session directory::
#
#     {DEFAULT_WORKSPACE_ROOT}/.agent_sessions/{session_id}/
#         rollout.jsonl     # the append-only truth source (liveness signal)
#         blobs/            # file snapshots
#         tool_results/     # large tool-result overflow
#         task_outputs/     # background-task stdout logs
#
# Because artifacts are subordinate to the session directory, deleting a
# session directory removes its whole footprint atomically — cleanup is
# orphan-proof by construction. These names are the single source of truth for
# the layout; :class:`mote.runtime.workspace.WorkspaceStore` composes them and is
# the only module that turns them into paths.
# ============================================================================
#: Directory (under the workspace root) that holds every session directory.
SESSIONS_SUBDIR = ".agent_sessions"
#: The rollout log file inside each session directory (the truth source).
ROLLOUT_FILENAME = "rollout.jsonl"
#: Bucket used when a caller has no session id (shared, unattributed artifacts).
DEFAULT_SESSION_BUCKET = "default"
#: Throttle stamp file (under the workspace root) for the periodic cleanup sweep.
WORKSPACE_CLEANUP_STAMP = ".last_cleanup"

# Pre-co-location, top-level artifact trees. New writes co-locate under the
# session directory; these constants exist only so the cleanup sweep can
# recognize and mtime-prune leftover legacy data one last time.
LEGACY_TOOL_RESULTS_SUBDIR = ".tool_results"
LEGACY_TASK_OUTPUTS_SUBDIR = ".task_outputs"

# The trusted PROJECT config lives under ``~/.mote`` (CONFIG_ROOT), same dir as
# the user config — that is where ``config.yaml`` is shipped/edited.
SOURCE_ROOT = CONFIG_ROOT


# ============================================================================
# ``.mote`` project-dir discovery
# ----------------------------------------------------------------------------
# Skills / agents / mcp / settings are discovered by walking from the working
# directory *up* to the git root, collecting every ``<dir>/.mote/<subdir>`` that
# exists. Stopping at the git root prevents assets from parent directories
# outside the repo from leaking in.
# ============================================================================
def user_mote_dir(subdir: str) -> Path:
    """The user-level ``~/.mote/<subdir>`` location (lowest project-band layer)."""
    return CONFIG_ROOT / subdir


def mote_project_dirs(subdir: str, cwd: Optional[Path] = None) -> List[Path]:
    """Existing ``<dir>/.mote/<subdir>`` dirs from the git root down to *cwd*.

    Walks upward from *cwd* collecting each existing ``<dir>/.mote/<subdir>``,
    stopping at (and including) the git root — or, when *cwd* is not inside a
    repo, at the filesystem root. Returned **low→high precedence** (git root
    first, *cwd* last), so a caller can let a closer-to-cwd directory override a
    farther one. Uses a per-project upward walk with a git-root boundary.

    Best-effort and side-effect-free: only directories that actually exist are
    returned; the list may be empty.
    """
    start = Path(cwd) if cwd is not None else Path.cwd()
    try:
        start = start.resolve()
    except OSError:
        start = start.absolute()

    git_root_str = find_git_root(str(start))
    stop = Path(git_root_str).resolve() if git_root_str else None

    # Collect from cwd upward (high→low), then reverse to low→high.
    collected: List[Path] = []
    current = start
    while True:
        candidate = current / MOTE_DIR_NAME / subdir
        if candidate.is_dir():
            collected.append(candidate)
        # Stop after processing the boundary (git root), or at fs root.
        if stop is not None and current == stop:
            break
        parent = current.parent
        if parent == current:  # reached filesystem root
            break
        current = parent

    collected.reverse()  # low→high precedence (git root first, cwd last)
    return collected


def mote_project_files(filename: str, cwd: Optional[Path] = None) -> List[Path]:
    """Existing ``<dir>/.mote/<filename>`` files from the git root down to *cwd*.

    The file-oriented sibling of :func:`mote_project_dirs` (e.g. for
    ``.mote/mcp.json`` / ``.mote/settings.local.json``). Same upward walk with a
    git-root boundary; returned **low→high precedence** (git root first, *cwd*
    last) so a closer file overrides a farther one.
    """
    start = Path(cwd) if cwd is not None else Path.cwd()
    try:
        start = start.resolve()
    except OSError:
        start = start.absolute()

    git_root_str = find_git_root(str(start))
    stop = Path(git_root_str).resolve() if git_root_str else None

    collected: List[Path] = []
    current = start
    while True:
        candidate = current / MOTE_DIR_NAME / filename
        if candidate.is_file():
            collected.append(candidate)
        if stop is not None and current == stop:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    collected.reverse()
    return collected


def mote_layered_files(filename: str, cwd: Optional[Path] = None) -> List[Path]:
    """All ``.mote/<filename>`` config files to read, low→high precedence.

    ``~/.mote/<filename>`` (user) first, then every ``<dir>/.mote/<filename>``
    found walking from *cwd* up to the git root (closer-to-cwd last, so it wins).
    Only existing files are returned; the list may be empty. The shared discovery
    order behind the ``.mote/`` JSON config sources (permission settings, MCP).
    """
    paths: List[Path] = []
    user_file = CONFIG_ROOT / filename
    if user_file.is_file():
        paths.append(user_file)
    paths.extend(mote_project_files(filename, cwd))
    return paths


def load_mote_json_section(path: Path, top_key: str, log_prefix: str) -> dict:
    """Read one ``.mote/`` JSON file and return its *top_key* object (best-effort).

    Shared by the ``.mote/`` JSON config sources (permission ``settings.local.json``
    → ``permissions``; MCP ``mcp.json`` → ``mcpServers``). A missing / empty /
    malformed file, or a top-level shape mismatch, yields an empty dict rather
    than raising, so a single bad file never breaks discovery. *log_prefix* tags
    the warning lines (e.g. ``"settings"`` / ``"MCP config"``).
    """
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning(f"{log_prefix}: could not read {path}: {exc}")
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(f"{log_prefix}: {path} is not valid JSON: {exc}")
        return {}
    section = data.get(top_key) if isinstance(data, dict) else None
    if not isinstance(section, dict):
        logger.warning(f"{log_prefix}: {path} has no '{top_key}' object.")
        return {}
    return section


def mote_source_dirs(subdir: str, cwd: Optional[Path] = None) -> List[Path]:
    """Full layered source-dir list for *subdir* (low→high precedence).

    ``~/.mote/<subdir>`` (user) first, then the project upward-walk
    (:func:`mote_project_dirs`). Non-existent user dir is still returned so
    callers that create-on-demand see a stable base; project dirs are filtered
    to existing ones.
    """
    return [user_mote_dir(subdir), *mote_project_dirs(subdir, cwd)]
