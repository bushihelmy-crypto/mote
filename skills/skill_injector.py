"""Inject Skills metadata and alwaysApply instructions into system prompts."""

from pathlib import Path
from typing import Optional

from metagpt.common.logs import logger
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

    def __init__(self, pool: SkillPool, skills_md_path: Optional[Path] = None):
        self._pool = pool
        self._skills_md_path = skills_md_path

    def build_content(self, max_tokens: int = 2000) -> str:
        """Build Skills injection content without appending to a prompt.

        Returns:
            Skills content string, or empty string if no skills available.
        """
        if self._pool.get_skill_count() == 0:
            return ""

        parts = []

        # 1. SKILLS.md index (sanitized like alwaysApply content)
        index_content = self._read_skills_index()
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
            logger.warning(
                f"SkillInjector: injection content ({token_count} tokens) exceeds "
                f"max_tokens ({max_tokens}), truncating"
            )
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

    def _read_skills_index(self) -> str:
        """Read SKILLS.md content, stripping YAML frontmatter."""
        if not self._skills_md_path or not self._skills_md_path.exists():
            return ""
        try:
            text = self._skills_md_path.read_text(encoding="utf-8")
            # Strip frontmatter
            if text.startswith("---"):
                end = text.find("---", 3)
                if end != -1:
                    text = text[end + 3:].lstrip("\n")
            return text.strip()
        except Exception as e:
            logger.warning(f"SkillInjector: failed to read SKILLS.md: {e}")
            return ""

    # sanitize and truncate_to_tokens are provided by metagpt.common.utils.prompt_sanitizer
