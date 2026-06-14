"""Inject Skills metadata and alwaysApply instructions into system prompts."""

from metagpt.skills.skill_pool import SkillPool
from metagpt.common.utils.prompt_sanitizer import count_tokens, sanitize, truncate_to_tokens

_LOADING_GUIDE = (
    "## Skill Loading Guide\n"
    "To use a Skill listed in the index above, read its full instructions with:\n"
    "  Editor.read(\"<Path from the table above>\")\n"
    "Load Skills on-demand based on the current task requirements.\n"
    "If a Skill covers the same capability as a tool, prefer following the Skill instructions."
)


class SkillInjector:
    """Inject Skills index + alwaysApply instructions into system prompts."""

    def __init__(self, pool: SkillPool):
        self._pool = pool

    def build_content(self, max_tokens: int = 2000) -> str:
        """Build Skills injection content without appending to a prompt.

        Returns:
            Skills content string, or empty string if no skills available.
        """
        if self._pool.get_skill_count() == 0:
            return ""

        parts = []

        # 1. Skills index built in-memory from the pool (sanitized like alwaysApply content)
        index_content = self._build_index()
        if index_content:
            parts.append(f"## Available Skills\n{sanitize(index_content)}")

        # 2. alwaysApply Skills
        always_active = [s for s in self._pool.get_all() if s.always_apply]
        if always_active:
            active_parts = ["## Always Active Skills"]
            for skill in always_active:
                sanitized = sanitize(skill.instructions)
                active_parts.append(f"### {skill.name}\n{sanitized}")
            parts.append("\n\n".join(active_parts))

        # 3. Loading guide
        parts.append(_LOADING_GUIDE)

        injection = "\n\n".join(parts)

        # Token control
        token_count = count_tokens(injection)
        if token_count > max_tokens:
            injection = truncate_to_tokens(injection, max_tokens)

        return injection

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

    def _build_index(self) -> str:
        """Build the Skills index table in-memory from the pool.

        Paths point at each Skill's source SKILL.md in the builtin directory so
        the agent can load full instructions on-demand via ``Editor.read()``.
        """
        skills = self._pool.get_all()
        if not skills:
            return ""
        lines = [
            "The following Skills are available in this project. Use `Editor.read()`",
            "to load the full SKILL.md for detailed instructions when needed.",
            "",
            "| Skill | Description | Path |",
            "|-------|-------------|------|",
        ]
        for s in skills:
            safe_desc = s.description.replace("\n", " ").replace("|", r"\|")
            lines.append(f"| {s.name} | {safe_desc} | {s.source_path} |")
        return "\n".join(lines)

    # sanitize and truncate_to_tokens are provided by metagpt.common.utils.prompt_sanitizer
