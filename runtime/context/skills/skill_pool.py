"""Skill pool: loads and manages Skills from layered source directories."""

from pathlib import Path
from typing import Optional

from mote.runtime.context.skills.audit import audit_skill_body
from mote.runtime.context.skills.markdown import MarkdownMetaParser
from mote.runtime.context.skills.skill_definition import SkillDefinition
from mote.runtime.logging import logger

# Default skills directory relative to this package (the lowest-priority,
# bundled layer).
_BUILTIN_DIR = Path(__file__).parent / "yamls"


class SkillPool:
    """Load and manage Skills from one or more layered source directories.

    Source directories are scanned in *precedence* order (lowest first): a
    later directory overrides an earlier one for the same skill name, mirroring
    the config-center's precedence-as-data layering
    (``bundled < user < project``). Physical directories are de-duplicated by
    realpath so the same dir listed twice is scanned once.
    """

    def __init__(
        self,
        builtin_dir: Optional[Path] = None,
        *,
        source_dirs: Optional[list[Path]] = None,
    ):
        self._skills: dict[str, SkillDefinition] = {}
        self._parser = MarkdownMetaParser()
        # ``source_dirs`` (lowest-priority first) is the canonical input; the
        # ``builtin_dir`` kwarg is an accepted single-layer alternative.
        if source_dirs is not None:
            self._source_dirs: list[Path] = list(source_dirs)
        elif builtin_dir is not None:
            self._source_dirs = [builtin_dir]
        else:
            self._source_dirs = [_BUILTIN_DIR]

    @property
    def builtin_dir(self) -> Path:
        """The lowest-priority (first) source directory.

        A single-directory convenience accessor; prefer :attr:`source_dirs`.
        """
        return self._source_dirs[0]

    @property
    def source_dirs(self) -> list[Path]:
        """All source directories scanned, lowest-priority first."""
        return list(self._source_dirs)

    def load_all(self):
        """Load every skill discovered across all source directories."""
        self._skills.clear()
        for skill_dir in self._scan_available().values():
            self._load_skill_from_dir(skill_dir)

    def load_by_names(self, names: list[str]):
        """Load specific skills by name from the source directories.

        Args:
            names: List of skill names to load.
        """
        self._skills.clear()
        available = self._scan_available()
        for name in names:
            skill_dir = available.get(name)
            if skill_dir is None:
                continue
            self._load_skill_from_dir(skill_dir)

    def _scan_available(self) -> dict[str, Path]:
        """Map each skill directory name to its path across all source dirs.

        Directories are scanned lowest-priority first, so a higher-priority
        layer's skill of the same name overwrites the entry. Skills nested
        under an underscore-prefixed directory are skipped. Physical source
        directories are de-duplicated by realpath.
        """
        available: dict[str, Path] = {}
        seen_roots: set[str] = set()
        for root in self._source_dirs:
            if not root.is_dir():
                continue
            try:
                key = str(root.resolve())
            except OSError:
                key = str(root)
            if key in seen_roots:
                continue
            seen_roots.add(key)
            for skill_md in sorted(root.rglob("SKILL.md")):
                parent_parts = skill_md.relative_to(root).parts[:-1]
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
                globs=meta.get("globs", []),
                instructions=doc.content,
                source_path=skill_md,
                metadata=meta,
                # supported frontmatter (accepts both hyphenated and
                # snake_case keys; all optional with safe defaults).
                when_to_use=meta.get("when_to_use", meta.get("when-to-use", "")),
                context=meta.get("context", "inline"),
                allowed_tools=meta.get("allowed_tools", meta.get("allowed-tools", [])),
                model=meta.get("model", ""),
                effort=meta.get("effort", ""),
                argument_hint=meta.get("argument_hint", meta.get("argument-hint", "")),
                disable_model_invocation=meta.get("disable_model_invocation", False),
                paths=meta.get("paths", []),
            )
        except Exception as exc:  # noqa: BLE001 — a malformed skill is skipped, not fatal
            logger.warning(f"Skipping malformed skill at {skill_md}: {exc}")
            return

        if not skill.is_valid():
            logger.debug(f"Skipping skill at {skill_md}: invalid name/description")
            return

        # Supply-chain gate: the body is injected verbatim (inline) or run as a
        # child-agent prompt (fork), so a hostile body is refused before it can
        # ever reach a prompt. CRITICAL → skip; softer findings → warn + keep.
        report = audit_skill_body(skill.instructions)
        if report.has_findings:
            if report.ok:
                logger.warning(f"Skill {skill.name!r} at {skill_md} audit warnings: {report.summary()}")
            else:
                logger.warning(f"Refusing skill {skill.name!r} at {skill_md} — audit blocked: {report.summary()}")
                return

        self._skills[skill.name] = skill

    def get(self, name: str) -> Optional[SkillDefinition]:
        """Return a loaded skill by name, or ``None`` if not loaded."""
        return self._skills.get(name)

    def get_all(self) -> list[SkillDefinition]:
        """Return all loaded Skills."""
        return list(self._skills.values())

    def get_skill_count(self) -> int:
        return len(self._skills)
