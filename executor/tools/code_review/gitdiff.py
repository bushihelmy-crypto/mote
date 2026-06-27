"""Fetch a git diff for the code-review pipeline.

Three modes, selected by the arguments supplied:

* ``commit`` given            → ``git show <commit>`` (review a single commit)
* ``from_ref`` and ``to_ref`` → ``git diff <from>..<to>`` (review a range)
* neither                     → ``git diff HEAD`` (review the working tree)

Shells out via :func:`metagpt.common.utils.common.aexecute` (``wait=True`` →
``(rc, out, err)``). Best-effort: a non-zero git exit returns whatever git
wrote to stdout (often empty) rather than raising, so the pipeline degrades to
"no files to review" instead of crashing the background task.
"""
from __future__ import annotations

import shlex
from typing import Optional

from metagpt.common.utils.common import aexecute


def _build_command(
    from_ref: Optional[str],
    to_ref: Optional[str],
    commit: Optional[str],
) -> str:
    """Build the git command string for the requested diff scope."""
    if commit:
        return f"git show --no-color {shlex.quote(commit)}"
    if from_ref and to_ref:
        return (
            f"git diff --no-color {shlex.quote(from_ref)}..{shlex.quote(to_ref)}"
        )
    if from_ref:
        # Single ref given — diff it against the working tree.
        return f"git diff --no-color {shlex.quote(from_ref)}"
    return "git diff --no-color HEAD"


async def get_diff(
    repo_dir: str,
    from_ref: Optional[str] = None,
    to_ref: Optional[str] = None,
    commit: Optional[str] = None,
) -> str:
    """Return the unified-diff text for the requested scope (best-effort).

    Args:
        repo_dir: Working directory of the git repository.
        from_ref: Base ref for a range diff (e.g. a branch or SHA).
        to_ref: Target ref for a range diff. Requires ``from_ref``.
        commit: A single commit to review via ``git show``. Takes precedence
            over the ref range.

    Returns:
        The diff text (stdout). Empty string when git fails or there is no diff.
    """
    cmd = _build_command(from_ref, to_ref, commit)
    result = await aexecute(cmd, working_dir=repo_dir, wait=True)
    if not result:
        return ""
    _rc, out, _err = result
    return out or ""
