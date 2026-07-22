"""Dependency-free filesystem probes for version-control worktrees."""
from __future__ import annotations

import os
from typing import Optional


def find_git_dir(start: str) -> Optional[tuple[str, str]]:
    """Return ``(worktree root, git directory)`` for *start*, if any."""
    try:
        current = os.path.abspath(start)
    except (OSError, ValueError):
        return None
    while True:
        dotgit = os.path.join(current, ".git")
        if os.path.isdir(dotgit):
            return current, dotgit
        if os.path.isfile(dotgit):
            git_dir = _resolve_gitfile(dotgit, current)
            return current, (git_dir or dotgit)
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def find_git_root(cwd: str) -> Optional[str]:
    """Return the Git worktree root containing *cwd*, without subprocesses."""
    found = find_git_dir(cwd)
    return found[0] if found is not None else None


def _resolve_gitfile(dotgit_file: str, base: str) -> Optional[str]:
    try:
        with open(dotgit_file, encoding="utf-8") as file:
            content = file.read().strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not content.startswith(prefix):
        return None
    path = content[len(prefix) :].strip()
    if not os.path.isabs(path):
        path = os.path.normpath(os.path.join(base, path))
    return path


__all__ = ["find_git_dir", "find_git_root"]
