"""Render a :class:`GitState` into environment-section lines.

Produces the ``# Environment`` git block (branch / status / recent commits) that
PromptBuilder splices below the system-prompt cache boundary, so the per-turn
changing state never busts the cacheable prefix.
"""
from __future__ import annotations

from metagpt.common.utils.git_state.collector import GitState


def render_git_section(state: GitState) -> str:
    """Render *state* as a multi-line git block, or "" when there's nothing useful."""
    if state is None:
        return ""

    head = state.branch or (f"detached @ {state.detached_sha}" if state.detached_sha else "unknown")

    if state.clean:
        status = "clean"
    else:
        bits = []
        if state.staged:
            bits.append(f"{state.staged} staged")
        if state.unstaged:
            bits.append(f"{state.unstaged} unstaged")
        if state.untracked:
            bits.append(f"{state.untracked} untracked")
        status = "dirty (" + ", ".join(bits) + ")"

    lines = [
        f" - Git branch: {head}",
        f" - Git status: {status}",
    ]
    if state.recent_commits:
        lines.append(" - Recent commits:")
        lines.extend(f"     {c}" for c in state.recent_commits)
    return "\n".join(lines)
