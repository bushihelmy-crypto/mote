"""Inject Skills index into system prompts.

The steady index lists *model-invocable, non-conditional* skills only (so the
cacheable system-prompt prefix stays stable): ``disable_model_invocation``
skills are hidden, and skills gated behind ``paths``/``globs`` are surfaced
per-turn by the conditional-activation source (see ``turn_context``) instead of
here. Skill bodies are never inlined into the prompt — they load on demand via
the ``Skill`` tool.

When the index would blow the token budget it degrades in three tiers
(full description → half description → name only), mirroring claude-code.
"""

from typing import Optional

from mote.common.utils.prompt_sanitizer import count_tokens, sanitize
from mote.context.skills.skill_pool import SkillPool

_LOADING_GUIDE = (
    "## Skill Loading Guide\n"
    "To use a Skill listed in the index above, invoke the `Skill` tool:\n"
    '  Skill(name="<skill-name>", arguments="<your input>")\n'
    "The Skill runs on-demand and its instructions/result come back as the tool "
    "result (inline skills) or as a summary from an isolated sub-agent (fork "
    "skills). To discover skills not shown in the index, search with "
    'Skill(query="<keywords>").\n'
    "If a Skill covers the same capability as a tool, prefer following the Skill."
)


class SkillInjector:
    """Inject the Skills index into system prompts."""

    def __init__(self, pool: SkillPool):
        self._pool = pool

    def _index_skills(self, only_names: Optional[set] = None) -> list:
        """Skills eligible for the steady, model-facing index.

        Excludes conditional (path/glob-gated, surfaced per-turn) and
        human-only skills. When ``only_names`` is given, the result is further
        restricted to skills whose name is in that set (order preserved) — used
        by the per-turn listing source to render only the newly-added skills.
        """
        skills = [s for s in self._pool.get_all() if not s.is_conditional and not s.disable_model_invocation]
        if only_names is not None:
            skills = [s for s in skills if s.name in only_names]
        return skills

    def build_guide(self) -> str:
        """The static Skill Loading Guide — belongs in the cacheable system prompt.

        Constant per session (it merely explains how to invoke the ``Skill``
        tool), so it stays in the prompt prefix rather than being re-sent every
        turn. Empty string when no skills are available.
        """
        if self._pool.get_skill_count() == 0:
            return ""
        return _LOADING_GUIDE

    def build_index(self, max_tokens: int = 2000, only_names: Optional[set] = None) -> str:
        """The volatile ``## Available Skills`` index block (no loading guide).

        Delivered per-turn by :class:`SkillListingContextSource` (never the
        cacheable prompt) because skills hot-reload. Degrades across three tiers
        to fit ``max_tokens``.

        Args:
            max_tokens: Max tokens for the index (degrades to fit).
            only_names: When given, render only skills whose name is in this set
                (incremental delta rendering for the listing source).

        Returns:
            The index block, or empty string when there is nothing to show.
        """
        if self._pool.get_skill_count() == 0:
            return ""

        index_skills = self._index_skills(only_names)
        if not index_skills:
            return ""
        index_block = self._build_index_within_budget(index_skills, max_tokens)
        if not index_block:
            return ""
        return f"## Available Skills\n{sanitize(index_block)}"

    def build_content(self, max_tokens: int = 2000) -> str:
        """Index + loading guide, joined — the full injectable block.

        Convenience composition of :meth:`build_index` and :meth:`build_guide`,
        kept for :meth:`inject` and standalone rendering. The steady runtime
        splits these two across the system prompt (guide) and the per-turn
        reminder (index) instead.
        """
        parts = [p for p in (self.build_index(max_tokens), self.build_guide()) if p]
        return "\n\n".join(parts)

    def inject(self, system_prompt: str, max_tokens: int = 2000) -> str:
        """Append Skills information to the system prompt.

        Args:
            system_prompt: The base system prompt.
            max_tokens: Max tokens for injected content.

        Returns:
            Enhanced system prompt with Skills content appended.
        """
        injection = self.build_content(max_tokens)
        if not injection:
            return system_prompt
        return f"{system_prompt}\n\n{injection}"

    def _build_index_within_budget(self, skills: list, budget: int) -> str:
        """Build the index, degrading description detail to fit ``budget``.

        Tier 0: full description; Tier 1: half description; Tier 2: name only.
        The lowest tier is emitted unconditionally even if it overflows (an
        index of names is always more useful than nothing).
        """
        if not skills:
            return ""
        # Try richer tiers first; tier 2 (name-only) is the floor and is emitted
        # unconditionally — an index of names is always more useful than nothing.
        for tier in (0, 1):
            block = self._build_index(skills, tier)
            if count_tokens(block) <= budget:
                return block
        return self._build_index(skills, 2)

    def _build_index(self, skills: Optional[list] = None, tier: int = 0) -> str:
        """Build the Skills index in-memory from the pool.

        ``tier`` controls verbosity: 0 = full description (+ argument hint),
        1 = half description, 2 = name only. Skills are loaded on-demand via the
        ``Skill`` tool, so no source path is shown.
        """
        if skills is None:
            skills = self._index_skills()
        if not skills:
            return ""

        if tier >= 2:
            lines = [
                "The following Skills are available (invoke via the `Skill` tool):",
                "",
            ]
            lines.extend(f"- {s.name}" for s in skills)
            return "\n".join(lines)

        lines = [
            "The following Skills are available. Invoke one with",
            '`Skill(name="<skill>", arguments="...")` when relevant.',
            "",
            "| Skill | Description | Arguments |",
            "|-------|-------------|-----------|",
        ]
        for s in skills:
            desc = self._describe(s, tier)
            safe_desc = desc.replace("\n", " ").replace("|", r"\|")
            args = (s.argument_hint or "").replace("\n", " ").replace("|", r"\|")
            lines.append(f"| {s.name} | {safe_desc} | {args} |")
        return "\n".join(lines)

    @staticmethod
    def _describe(skill, tier: int) -> str:
        """Compose a skill's index description (merging when_to_use) per tier."""
        desc = skill.description or ""
        if skill.when_to_use:
            desc = f"{desc} (use when: {skill.when_to_use})" if desc else skill.when_to_use
        if tier >= 1 and len(desc) > 0:
            half = max(1, len(desc) // 2)
            desc = desc[:half].rstrip() + "…"
        return desc

    # sanitize and count_tokens are provided by mote.common.utils.prompt_sanitizer
