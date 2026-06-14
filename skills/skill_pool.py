"""Skill pool: loads and manages builtin Skills."""

from pathlib import Path
from typing import Optional

from metagpt.common.logs import logger
from metagpt.common.utils.markdown_meta_parser import MarkdownMetaParser
from metagpt.skills.skill_definition import SkillDefinition

# Default builtin directory relative to this package
_BUILTIN_DIR = Path(__file__).parent / "builtin"


class SkillPool:
    """Load and manage builtin Skills from the filesystem."""

    def __init__(self, builtin_dir: Optional[Path] = None):
        self._skills: dict[str, SkillDefinition] = {}
        self._builtin_dir: Path = builtin_dir or _BUILTIN_DIR
        self._parser = MarkdownMetaParser()

    def load_by_names(self, names: list[str]):
        """Load specific skills by name from the builtin directory.

        Args:
            names: List of skill names to load.
        """
        self._skills.clear()
        available = self._scan_available()
        for name in names:
            skill_dir = available.get(name)
            if skill_dir is None:
                logger.debug(f"SkillPool: skill '{name}' not found in builtin dir")
                continue
            self._load_skill_from_dir(skill_dir)

    def _scan_available(self) -> dict[str, Path]:
        """Map each skill directory name to its path for every SKILL.md under builtin/.

        Skills nested under an underscore-prefixed directory are skipped.
        """
        available: dict[str, Path] = {}
        if not self._builtin_dir.is_dir():
            return available
        for skill_md in sorted(self._builtin_dir.rglob("SKILL.md")):
            parent_parts = skill_md.relative_to(self._builtin_dir).parts[:-1]
            if any(part.startswith("_") for part in parent_parts):
                continue
            available[skill_md.parent.name] = skill_md.parent
        return available

    def _load_skill_from_dir(self, skill_dir: Path):
        """Parse a SKILL.md into a SkillDefinition and register it."""
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            return

        try:
            doc = self._parser.parse(skill_md)
            meta = doc.metadata
            skill = SkillDefinition(
                name=meta.get("name", skill_dir.name),
                description=meta.get("description", ""),
                always_apply=meta.get("alwaysApply", False),
                globs=meta.get("globs", []),
                roles=meta.get("roles", []),
                instructions=doc.content,
                source_path=skill_md,
                metadata=meta,
            )
        except Exception as e:
            logger.warning(f"SkillPool: failed to load {skill_md}: {e}")
            return

        if not skill.is_valid():
            logger.warning(f"SkillPool: invalid skill definition in {skill_md} (name={skill.name!r})")
            return

        self._skills[skill.name] = skill

    def get_all(self) -> list[SkillDefinition]:
        """Return all loaded Skills."""
        return list(self._skills.values())

    def get_skill_count(self) -> int:
        return len(self._skills)
