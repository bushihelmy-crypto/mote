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

After a durable model-context rebuild commits, the context domain invokes
``on_model_context_rebuilt`` directly before publishing the new live view. The
callback resets the frontier so the next turn re-emits the full index; this
correctness step does not travel through lossy telemetry.

It holds a callable returning a narrow injector Protocol, so the low ``context``
layer never imports the skill manager or the Role. ``get_injector()`` yields the
live injector capability (or ``None`` when
skills are disabled / not yet ready).
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional, Protocol

from mote.contracts.events.conversation import MODEL_CONTEXT_REBUILT_EVENTS, ModelContextRebuiltEvent
from mote.contracts.ports.conversation.turn_context import TurnContextPriority


class _IndexedSkill(Protocol):
    """The one field this source reads off an eligible skill (its name)."""

    name: str


class _SkillInjector(Protocol):
    """The exact skill-index slice consumed by this source."""

    def _index_skills(self) -> Iterable[_IndexedSkill]: ...

    def build_index(self, *, max_tokens: int = ..., only_names: Optional[set[str]] = ...) -> Optional[str]: ...


class SkillListingContextSource:
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
        is_enabled: Callable[[], bool],
    ) -> None:
        self._get_injector = get_injector
        self._max_tokens = max_tokens
        self._is_enabled = is_enabled
        # Names already surfaced in a prior turn — the incremental frontier.
        self._sent_names: set[str] = set()

    async def on_model_context_rebuilt(self, event: ModelContextRebuiltEvent) -> None:
        """Reset the incremental frontier when stored history is structurally rebuilt.

        Two orthogonal causes fold to the same fix (``MODEL_CONTEXT_REBUILT_EVENTS``):
        a compaction condenses the prior full listing away, and a ``/clear`` or a
        user delete prunes the messages that carried it. In every case the model
        no longer has the listing, so clearing ``_sent_names`` makes the next
        render re-diff against the live skill set — re-emitting the whole index
        (or, after a delete that left some skills still announced elsewhere, only
        the ones now missing). All other events ignored.
        """
        if isinstance(event, MODEL_CONTEXT_REBUILT_EVENTS):
            self._sent_names = set()

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        # Master switch off → never render, regardless of injector state.
        if not self._is_enabled():
            return None

        injector = self._get_injector()
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
