"""SkillListingContextSource — the steady Skills index, per turn.

The model-invocable, non-conditional Skills index is kept out of the
cacheable system prompt: skills hot-reload (a ``SKILL.md`` add/remove
re-scans the pool), and any change to the index would bust the whole cached
prompt prefix. So the volatile *index* is delivered per-turn in the user
prompt's ``<system-reminder>`` instead, while the *static loading guide* (how to
invoke the ``Skill`` tool — constant per session) stays in the system prompt.
The first turn emits the full index; every later turn emits only the
*newly-added* skills (tracked by ``_sent_names``). Removals are not re-announced
(the model simply stops seeing them; a stale attempt fails cleanly and
re-searches via ``Skill(query=...)``).

Push→pull bridge in one object (like :class:`ToolCatalogContextSource`, whose
incremental frontier is identical): as an
:class:`~mote.common.interface.ObservationSubscriber` it catches
:class:`~mote.common.events.PostCompactEvent` off the bus and resets the
frontier, so the turn after a compaction re-emits the *full* index (the earlier
full listing was persisted into history and condensed away with the rest of the
pre-compaction history — without the reset ``_sent_names`` would still believe
every skill was announced and the index would be silently, permanently lost).
As an :class:`~mote.common.interface.EphemeralContextSource` it renders the
index once per think() cycle.

Duck-typed (mirrors :class:`SkillActivationContextSource`): it holds a single
callable so the low ``context`` layer never imports the skill manager or the
Role. ``get_injector()`` yields the live :class:`SkillInjector` (or ``None`` when
skills are disabled / not yet ready).
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional, Protocol

from mote.common.events import PostCompactEvent
from mote.common.interface import ObservationSubscriber, TurnContextPriority


class _IndexedSkill(Protocol):
    """The one field this source reads off an eligible skill (its name)."""

    name: str


class _SkillInjector(Protocol):
    """The skill-index slice this source reads off the injector (duck-typed).

    Structural only — keeps the low ``context`` layer from importing the skill
    manager; any object exposing these two members satisfies it.
    """

    def _index_skills(self) -> Iterable[_IndexedSkill]:
        ...

    def build_index(self, *, max_tokens: int = ..., only_names: Optional[set[str]] = ...) -> Optional[str]:
        ...


class SkillListingContextSource(ObservationSubscriber):
    """Emits the steady Skills index per turn, incrementally after the first."""

    name = "skill_listing"
    # Ahead of skill_activation: the steady index is the more fundamental
    # skill surface, so it renders first when both fire on the same turn.
    priority = TurnContextPriority.SKILL_LISTING
    save_to_context = True

    def __init__(
        self,
        get_injector: Callable[[], Optional[_SkillInjector]],
        *,
        max_tokens: int = 2000,
        is_enabled: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._get_injector = get_injector
        self._max_tokens = max_tokens
        # Optional master-switch gate (``config.context.skills.enabled``). When
        # supplied and False, the index never renders — on top of the original
        # self-suppression (no injector / no skills). None → gate on the original
        # logic alone (backwards-compatible).
        self._is_enabled = is_enabled
        # Names already surfaced in a prior turn — the incremental frontier.
        self._sent_names: set[str] = set()

    async def handle(self, event) -> None:
        """Reset the incremental frontier after a compaction (re-emit the full index).

        The prior full listing was persisted into history and condensed away by
        the compaction, so the model no longer has it; clearing ``_sent_names``
        makes the next render re-emit the whole index. All other events ignored.
        """
        if isinstance(event, PostCompactEvent):
            self._sent_names = set()
        return None

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        # Master switch off → never render, regardless of injector state.
        if self._is_enabled is not None and not self._is_enabled():
            return None

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
