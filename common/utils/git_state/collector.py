"""Read-only git working-tree state — for injection into the environment section.

Filesystem-first: the current branch is read straight from ``.git/HEAD`` with no
subprocess, while the porcelain status and the
recent-commit list shell out to ``git`` (those need git's own semantics). Everything
is best-effort: a missing repo, a missing ``git`` binary, or any failure degrades to
``None`` / empty fields rather than raising — this runs on the per-turn prompt-build
path and must never break a turn.

A short TTL cache keyed by the resolved repo root avoids re-running ``git`` on every
think cycle within the same second.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional

from mote.common.utils.common import aexecute
from mote.common.vcs import find_git_dir

# How long a collected snapshot stays fresh (seconds). The collector runs once per
# think cycle; a small TTL coalesces bursts without showing stale state for long.
_CACHE_TTL_S = 1.5
# Tight timeout for each git invocation — this is on the prompt-build path.
_GIT_TIMEOUT_S = 2.0
# How many recent commits to surface.
_RECENT_COMMITS = 5

# repo_root -> (monotonic_deadline, GitState)
_cache: dict[str, tuple[float, "GitState"]] = {}


@dataclass
class GitState:
    """An immutable snapshot of a repository's working-tree state."""

    repo_root: str
    branch: Optional[str] = None  # branch name, or None when detached
    detached_sha: Optional[str] = None  # short sha when HEAD is detached
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    recent_commits: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.staged == 0 and self.unstaged == 0 and self.untracked == 0


def _read_branch(git_dir: str) -> tuple[Optional[str], Optional[str]]:
    """Read HEAD filesystem-first. Returns (branch, detached_short_sha)."""
    try:
        with open(os.path.join(git_dir, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
    except OSError:
        return None, None
    if head.startswith("ref:"):
        ref = head[4:].strip()
        if ref.startswith("refs/heads/"):
            return ref[len("refs/heads/") :], None
        return ref or None, None
    # Detached HEAD: HEAD holds a raw sha.
    sha = head.strip()
    return None, (sha[:8] if sha else None)


async def _git(cwd: str, args: str) -> Optional[str]:
    """Run ``git <args>`` in *cwd*; return stdout (stripped) or None on any failure."""
    try:
        result = await aexecute(f"git {args}", working_dir=cwd, wait=True, timeout=_GIT_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — best-effort; never break the prompt build
        return None
    if not result:
        return None
    rc, stdout = result[0], result[1]
    if rc != 0:
        return None
    return stdout


def _parse_status(porcelain: str) -> tuple[int, int, int]:
    """Parse ``git status --porcelain`` into (staged, unstaged, untracked) counts."""
    staged = unstaged = untracked = 0
    for line in porcelain.splitlines():
        if len(line) < 2:
            continue
        x, y = line[0], line[1]
        if x == "?" and y == "?":
            untracked += 1
            continue
        if x not in (" ", "?"):
            staged += 1
        if y not in (" ", "?"):
            unstaged += 1
    return staged, unstaged, untracked


async def collect_git_state(cwd: str) -> Optional[GitState]:
    """Collect a read-only git snapshot for *cwd*, or None when not in a repo.

    Best-effort and cached with a short TTL. Never raises.
    """
    if not cwd:
        return None
    found = find_git_dir(cwd)
    if found is None:
        return None
    repo_root, git_dir = found

    now = time.monotonic()
    cached = _cache.get(repo_root)
    if cached is not None and cached[0] > now:
        return cached[1]

    branch, detached_sha = _read_branch(git_dir)

    porcelain = await _git(cwd, "status --porcelain")
    staged = unstaged = untracked = 0
    if porcelain is not None:
        staged, unstaged, untracked = _parse_status(porcelain)

    commits: list[str] = []
    log_out = await _git(cwd, f"log --oneline -{_RECENT_COMMITS}")
    if log_out:
        commits = [ln for ln in log_out.splitlines() if ln.strip()]

    state = GitState(
        repo_root=repo_root,
        branch=branch,
        detached_sha=detached_sha,
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        recent_commits=commits,
    )
    _cache[repo_root] = (now + _CACHE_TTL_S, state)
    return state
