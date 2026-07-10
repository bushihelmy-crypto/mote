"""SkillActivationContextSource — surface path-gated skills per turn.

Skills declaring ``paths`` / ``globs`` (``is_conditional``) are deliberately
kept OUT of the steady system-prompt index: changing that index whenever a file
is touched would bust the cacheable prompt prefix. Instead this ephemeral source
checks the files this session has recently touched (the ``record_file_read``
trajectory) against each conditional skill's activation patterns, and — only on
a match — emits that skill's index row into the per-turn ``<system-reminder>``
(request-only; never cached, never stored in history).

Duck-typed (mirrors :class:`TokenPressureContextSource`): it holds two plain
callables so the low ``context`` layer never imports the skill pool or the Role.
``get_pool()`` yields the live pool (or ``None`` when skills are disabled);
``get_touched_files()`` yields the recently-touched absolute file paths.
"""

from __future__ import annotations

import fnmatch
import os
from typing import Callable, Optional

from metagpt.common.interface import TurnContextPriority


class SkillActivationContextSource:
    """Emits index rows for conditional skills whose paths match touched files."""

    name = "skill_activation"
    # After git/token/compaction/bg/lsp — conditional skills are a low-urgency
    # hint, so they ride at the tail of the reminder.
    priority = TurnContextPriority.SKILL_ACTIVATION
    save_to_context = True

    def __init__(
        self,
        get_pool: Callable[[], object],
        get_touched_files: Callable[[], list],
    ) -> None:
        self._get_pool = get_pool
        self._get_touched_files = get_touched_files

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        pool = self._get_pool() if self._get_pool else None
        if pool is None:
            return None

        conditional = [
            s
            for s in pool.get_all()
            if getattr(s, "is_conditional", False)
            and not getattr(s, "disable_model_invocation", False)
        ]
        if not conditional:
            return None

        touched = self._get_touched_files() if self._get_touched_files else []
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
    def _matches(skill, touched: list, cwd: Optional[str]) -> bool:
        """True if any touched file matches any of the skill's activation patterns."""
        patterns = getattr(skill, "activation_patterns", None) or []
        if not patterns:
            return False
        for raw in touched:
            path = str(raw)
            base = os.path.basename(path)
            rel = path
            if cwd:
                try:
                    rel = os.path.relpath(path, cwd)
                except ValueError:  # different drive on Windows
                    rel = path
            for pat in patterns:
                if (
                    fnmatch.fnmatch(path, pat)
                    or fnmatch.fnmatch(rel, pat)
                    or fnmatch.fnmatch(base, pat)
                ):
                    return True
        return False

    @staticmethod
    def _row(skill) -> str:
        desc = getattr(skill, "description", "") or ""
        when = getattr(skill, "when_to_use", "") or ""
        if when:
            desc = f"{desc} (use when: {when})" if desc else when
        row = f"- {skill.name}: {desc}".rstrip()
        hint = getattr(skill, "argument_hint", "") or ""
        if hint:
            row += f" [args: {hint}]"
        return row


__all__ = ["SkillActivationContextSource"]
