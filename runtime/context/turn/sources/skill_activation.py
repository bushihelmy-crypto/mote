"""SkillActivationContextSource — surface path-gated skills per turn.

Skills declaring ``paths`` / ``globs`` (``is_conditional``) are deliberately
kept OUT of the steady system-prompt index: changing that index whenever a file
is touched would bust the cacheable prompt prefix. Instead this ephemeral source
checks the files this session has recently touched (the observed-snapshot
trajectory) against each conditional skill's activation patterns, and — only on
a match — emits that skill's index row into the per-turn ``<system-reminder>``
(request-only; never cached, never stored in history).

The source consumes two narrow structural contracts, so the low ``context``
layer never imports the skill pool or the Role.
``get_pool()`` yields the live pool (or ``None`` when skills are disabled);
``get_touched_files()`` yields the recently-touched absolute file paths.
"""

from __future__ import annotations

import fnmatch
import os
from typing import Callable, Iterable, Optional, Protocol

from mote.contracts.ports.conversation.turn_context import TurnContextPriority
from mote.runtime.file_paths import display_path


class _ConditionalSkill(Protocol):
    name: str
    description: str
    when_to_use: str
    argument_hint: str
    activation_patterns: tuple[str, ...] | list[str]
    is_conditional: bool
    disable_model_invocation: bool


class _SkillPool(Protocol):
    """The exact skill-pool query slice consumed by this source."""

    def get_all(self) -> Iterable[_ConditionalSkill]: ...


class SkillActivationContextSource:
    """Emits index rows for conditional skills whose paths match touched files."""

    name = "skill_activation"
    # After git/token/compaction/bg/lsp — conditional skills are a low-urgency
    # hint, so they ride at the tail of the reminder.
    priority = TurnContextPriority.SKILL_ACTIVATION
    # Ephemeral (request-only): a path-gated skill hint is a one-shot "this is
    # relevant right now" nudge tied to the files just touched. It is re-derived
    # every turn from the live touched-files set, so persisting it would only
    # freeze a stale match into history. Matches the docstring contract above
    # ("request-only; never stored in history").
    save_to_context = False

    def __init__(
        self,
        get_pool: Callable[[], Optional[_SkillPool]],
        get_touched_files: Callable[[], list[str]],
    ) -> None:
        self._get_pool = get_pool
        self._get_touched_files = get_touched_files

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        pool = self._get_pool()
        if pool is None:
            return None

        conditional = [s for s in pool.get_all() if s.is_conditional and not s.disable_model_invocation]
        if not conditional:
            return None

        touched = self._get_touched_files()
        if not touched:
            return None

        matched = [s for s in conditional if self._matches(s, touched, cwd)]
        if not matched:
            return None

        lines = [
            "# Relevant Skills",
            "These Skills match files you are working with. Invoke one with "
            '`Skill(name="<skill>", arguments="...")` when relevant.',
            "",
        ]
        for s in matched:
            lines.append(self._row(s))
        return "\n".join(lines)

    @staticmethod
    def _matches(
        skill: _ConditionalSkill,
        touched: list[str],
        cwd: Optional[str],
    ) -> bool:
        """True if any touched file matches any of the skill's activation patterns."""
        patterns = skill.activation_patterns
        if not patterns:
            return False
        for raw in touched:
            path = str(raw)
            base = os.path.basename(path)
            rel = display_path(path, cwd)
            for pat in patterns:
                if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(base, pat):
                    return True
        return False

    @staticmethod
    def _row(skill: _ConditionalSkill) -> str:
        desc = skill.description
        when = skill.when_to_use
        if when:
            desc = f"{desc} (use when: {when})" if desc else when
        row = f"- {skill.name}: {desc}".rstrip()
        hint = skill.argument_hint
        if hint:
            row += f" [args: {hint}]"
        return row


__all__ = ["SkillActivationContextSource"]
