"""Inject Skills metadata and alwaysApply instructions into system prompts.

The steady index lists *model-invocable, non-conditional* skills only (so the
cacheable system-prompt prefix stays stable): ``alwaysApply`` skills are emitted
in full under "Always Active Skills", ``disable_model_invocation`` skills are
hidden, and skills gated behind ``paths``/``globs`` are surfaced per-turn by the
conditional-activation source (see ``turn_context``) instead of here.

When the index would blow the token budget it degrades in three tiers
(full description → half description → name only), mirroring claude-code; the
full always-active bodies are preserved first.
"""

from metagpt.common.utils.prompt_sanitizer import count_tokens, sanitize
from metagpt.context.skills.skill_pool import SkillPool

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
    """Inject Skills index + alwaysApply instructions into system prompts."""

    def __init__(self, pool: SkillPool):
        self._pool = pool

    def _index_skills(self) -> list:
        """Skills eligible for the steady, model-facing index.

        Excludes alwaysApply (emitted in full elsewhere), conditional
        (path/glob-gated, surfaced per-turn) and human-only skills.
        """
        return [
            s
            for s in self._pool.get_all()
            if not s.always_apply
            and not s.is_conditional
            and not s.disable_model_invocation
        ]

    def build_content(self, max_tokens: int = 2000) -> str:
        """Build Skills injection content without appending to a prompt.

        Returns:
            Skills content string, or empty string if no skills available.
        """
        if self._pool.get_skill_count() == 0:
            return ""

        # 1. alwaysApply Skills (full body — preserved before degrading the index)
        always_section = self._build_always_active()
        # 2. Loading guide (fixed)
        guide = _LOADING_GUIDE

        fixed_parts = [p for p in (always_section, guide) if p]
        fixed_tokens = count_tokens("\n\n".join(fixed_parts)) if fixed_parts else 0
        index_budget = max(0, max_tokens - fixed_tokens)

        # 3. Skills index — degrade across three tiers to fit the budget.
        index_skills = self._index_skills()
        index_block = self._build_index_within_budget(index_skills, index_budget)

        parts = []
        if index_block:
            parts.append(f"## Available Skills\n{sanitize(index_block)}")
        if always_section:
            parts.append(always_section)
        parts.append(guide)
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

    def _build_always_active(self) -> str:
        """Render the full bodies of alwaysApply skills (sanitized)."""
        always_active = [s for s in self._pool.get_all() if s.always_apply]
        if not always_active:
            return ""
        active_parts = ["## Always Active Skills"]
        for skill in always_active:
            sanitized = sanitize(skill.instructions)
            active_parts.append(f"### {skill.name}\n{sanitized}")
        return "\n\n".join(active_parts)

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

    def _build_index(self, skills: list = None, tier: int = 0) -> str:
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

    # sanitize and count_tokens are provided by metagpt.common.utils.prompt_sanitizer
