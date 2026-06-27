"""GitContextSource — the git working-tree block as an ephemeral feed.

Migrated out of the system-prompt ``# Environment`` section into the unified
per-turn ephemeral layer: branch / dirty-clean status / recent commits now ride
in the cycle's ``<system-reminder>`` rather than the cacheable system prompt.
Best-effort — ``collect_git_state`` already returns ``None`` off-repo / on any
failure (and caches per repo_root for ~1.5s), so this is cheap to call each
cycle.

Change-gated (like the LSP feed): the block is emitted only when the working
tree actually changed since the last render — same branch / status / recent
commits stays silent so it doesn't repeat the same reminder every turn. When no
source has anything to report the bus drops the ``<system-reminder>`` entirely.
"""

from __future__ import annotations

from typing import Optional

from metagpt.common.utils.git_state import collect_git_state, render_git_section


class GitContextSource:
    """Renders the git status block on change for the live working directory."""

    name = "git"
    priority = 10
    save_to_context = True

    def __init__(self) -> None:
        # Last GitState we rendered; render again only when it differs (GitState
        # is a dataclass, so equality compares branch/status/commits structurally).
        self._last_state = None

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        state = await collect_git_state(cwd)
        if state is None:
            self._last_state = None
            return None
        if state == self._last_state:
            return None
        self._last_state = state
        return render_git_section(state) or None


__all__ = ["GitContextSource"]
