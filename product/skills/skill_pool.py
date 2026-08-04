"""Skill pool: loads and manages Skills from layered source directories."""

from collections.abc import Callable
from pathlib import Path
from typing import Optional

from mote.product.extensions.sources import ExtensionKind, ExtensionSource, ExtensionSourcePolicy
from mote.product.skills.audit import audit_skill_body
from mote.product.skills.markdown import MarkdownMetaParser
from mote.product.skills.skill_definition import (
    ActivatedSkillSnapshot,
    SkillContext,
    SkillManifest,
    SkillSourceEvidence,
)
from mote.runtime.context.token_budget import count_tokens
from mote.runtime.context.tokenizer import DEFAULT_TEXT_TOKENIZER
from mote.runtime.telemetry.logging import logger

# Default skills directory relative to this package (the lowest-priority,
# bundled layer).
_BUILTIN_DIR = Path(__file__).parent / "yamls"
_MANIFEST_KEYS = frozenset(
    {
        "name",
        "description",
        "globs",
        "when_to_use",
        "when-to-use",
        "context",
        "allowed_tools",
        "allowed-tools",
        "model",
        "effort",
        "argument_hint",
        "argument-hint",
        "disable_model_invocation",
        "paths",
    }
)


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
        source_policy: ExtensionSourcePolicy,
    ):
        self._skills: dict[str, ActivatedSkillSnapshot] = {}
        self._parser = MarkdownMetaParser()
        self._source_policy = source_policy
        self._token_counter: Callable[[str], int] = lambda text: count_tokens(text, tokenizer=DEFAULT_TEXT_TOKENIZER)
        self._tokenizer_identity = "mote.runtime.text-tokenizer/default-v1"
        self._tool_binding_generation = 1
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
        for source in self._scan_available().values():
            self._load_skill_from_source(source)

    def load_by_names(self, names: list[str]):
        """Load specific skills by name from the source directories.

        Args:
            names: List of skill names to load.
        """
        self._skills.clear()
        available = self._scan_available()
        for name in names:
            source = available.get(name)
            if source is None:
                continue
            self._load_skill_from_source(source)

    def _scan_available(self) -> dict[str, ExtensionSource]:
        """Map each skill directory name to its path across all source dirs.

        Directories are scanned lowest-priority first, so a higher-priority
        layer's skill of the same name overwrites the entry. Skills nested
        under an underscore-prefixed directory are skipped. Physical source
        directories are de-duplicated by realpath.
        """
        available: dict[str, ExtensionSource] = {}
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
            sources = self._source_policy.admitted_files(ExtensionKind.SKILL, sorted(root.rglob("SKILL.md")))
            for source in sources:
                skill_md = source.canonical_path
                parent_parts = skill_md.relative_to(root).parts[:-1]
                if any(part.startswith("_") for part in parent_parts):
                    continue
                available[skill_md.parent.name] = source
        return available

    def _load_skill_from_source(self, source: ExtensionSource) -> None:
        """Parse a SKILL.md into a SkillDefinition and register it."""
        skill_md = source.canonical_path
        skill_dir = skill_md.parent

        try:
            doc = self._parser.parse_text(source.content.decode("utf-8"), source_path=skill_md)
            meta = doc.metadata
            unknown = set(meta) - _MANIFEST_KEYS
            if unknown:
                raise ValueError(f"unsupported Skill manifest fields: {sorted(unknown)}")
            for snake, hyphen in (
                ("when_to_use", "when-to-use"),
                ("allowed_tools", "allowed-tools"),
                ("argument_hint", "argument-hint"),
            ):
                if snake in meta and hyphen in meta:
                    raise ValueError(f"duplicate Skill manifest field: {snake}/{hyphen}")
            allowed_tools = meta.get("allowed_tools", meta.get("allowed-tools", []))
            paths = meta.get("paths", [])
            globs = meta.get("globs", [])
            if not all(isinstance(value, list) for value in (allowed_tools, paths, globs)):
                raise ValueError("Skill list declarations must be arrays")
            patterns = tuple(dict.fromkeys((*paths, *globs)))
            manifest = SkillManifest(
                name=meta.get("name", skill_dir.name),
                description=meta.get("description", ""),
                when_to_use=meta.get("when_to_use", meta.get("when-to-use", "")),
                context=SkillContext(meta.get("context", "inline")),
                allowed_tools=tuple(allowed_tools),
                model=meta.get("model", ""),
                effort=meta.get("effort", ""),
                argument_hint=meta.get("argument_hint", meta.get("argument-hint", "")),
                disable_model_invocation=meta.get("disable_model_invocation", False),
                activation_patterns=patterns,
            )
            skill = ActivatedSkillSnapshot(
                manifest=manifest,
                source=SkillSourceEvidence(
                    canonical_path=skill_md,
                    content_digest=source.content_digest,
                    trust_decision="approved",
                    approval_generation=source.approval_generation,
                ),
                instructions=doc.content,
                tool_binding_generation=self._tool_binding_generation,
                tokenizer_identity=self._tokenizer_identity,
                token_cost=self._token_counter(doc.content),
            )
        except Exception as exc:  # noqa: BLE001 — a malformed skill is skipped, not fatal
            logger.warning(f"Skipping malformed skill at {skill_md}: {exc}")
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

    def get(self, name: str) -> Optional[ActivatedSkillSnapshot]:
        """Return a loaded skill by name, or ``None`` if not loaded."""
        return self._skills.get(name)

    def get_all(self) -> list[ActivatedSkillSnapshot]:
        """Return all loaded Skills."""
        return list(self._skills.values())

    def get_skill_count(self) -> int:
        return len(self._skills)
