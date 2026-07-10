"""SkillListingContextSource — the steady Skills index, per turn.

The model-invocable, non-conditional Skills index used to live entirely in the
cacheable system prompt. But skills hot-reload (a ``SKILL.md`` add/remove
re-scans the pool), and any change to the index would bust the whole cached
prompt prefix. So the volatile *index* is delivered per-turn in the user
prompt's ``<system-reminder>`` instead, while the *static loading guide* (how to
invoke the ``Skill`` tool — constant per session) stays in the system prompt.
The first turn emits the full index; every later turn emits only the
*newly-added* skills (tracked by ``_sent_names``). Removals are not re-announced
(the model simply stops seeing them; a stale attempt fails cleanly and
re-searches via ``Skill(query=...)``).

Duck-typed (mirrors :class:`SkillActivationContextSource`): it holds a single
callable so the low ``context`` layer never imports the skill manager or the
Role. ``get_injector()`` yields the live :class:`SkillInjector` (or ``None`` when
skills are disabled / not yet ready).
"""

from __future__ import annotations

from typing import Callable, Optional

from metagpt.common.interface import TurnContextPriority


class SkillListingContextSource:
    """Emits the steady Skills index per turn, incrementally after the first."""

    name = "skill_listing"
    # Ahead of skill_activation: the steady index is the more fundamental
    # skill surface, so it renders first when both fire on the same turn.
    priority = TurnContextPriority.SKILL_LISTING
    save_to_context = True

    def __init__(
        self,
        get_injector: Callable[[], object],
        *,
        max_tokens: int = 2000,
    ) -> None:
        self._get_injector = get_injector
        self._max_tokens = max_tokens
        # Names already surfaced in a prior turn — the incremental frontier.
        self._sent_names: set[str] = set()

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        injector = self._get_injector() if self._get_injector else None
        if injector is None:
            return None

        # The full set of skills eligible for the steady index right now.
        current = {s.name for s in injector._index_skills()}
        if not current:
            return None

        first_turn = not self._sent_names
        if first_turn:
            # Whole index (the static loading guide lives in the system prompt).
            content = injector.build_index(max_tokens=self._max_tokens)
            if not content:
                return None
            self._sent_names = set(current)
            return content

        # Incremental: only skills not yet announced.
        new_names = current - self._sent_names
        if not new_names:
            return None
        content = injector.build_index(max_tokens=self._max_tokens, only_names=new_names)
        if not content:
            return None
        self._sent_names |= new_names
        return f"# New Skills available\n{content}"


__all__ = ["SkillListingContextSource"]
