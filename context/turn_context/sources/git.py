"""GitContextSource — the git working-tree block as an ephemeral feed.

Migrated out of the system-prompt ``# Environment`` section into the unified
per-turn ephemeral layer: branch / dirty-clean status / recent commits now ride
in the cycle's ``<system-reminder>`` rather than the cacheable system prompt.
Best-effort — ``collect_git_state`` already returns ``None`` off-repo / on any
failure (and caches per repo_root for ~1.5s), so this is cheap to call each
cycle.
"""

from __future__ import annotations

from typing import Optional

from metagpt.common.git_state import collect_git_state, render_git_section


class GitContextSource:
    """Renders the git status block for the live working directory."""

    name = "git"
    priority = 10

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        state = await collect_git_state(cwd)
        if state is None:
            return None
        return render_git_section(state) or None


__all__ = ["GitContextSource"]
