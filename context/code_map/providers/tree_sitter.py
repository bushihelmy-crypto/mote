"""TreeSitterProvider — one :class:`LanguageProvider` class serving every
tree-sitter language, parameterized by a :class:`LangConfig`.

A tree-sitter language is *data*: the same class instance backs JavaScript, Go,
Rust, … differing only in the config it holds. :meth:`extract_tree` borrows a
thread-local parser from :mod:`~mote.context.code_map.ts_runtime`, runs a fresh
:class:`~mote.context.code_map.generic_builder.TreeSitterBuilder` over the tree,
and assembles the neutral :class:`ProviderExtract` — imports via the config's
declared extractor, the module summary from the leading comment block. Every
step is wrapped best-effort: a runtime that cannot parse (grammar absent) or a
builder that trips on an odd tree yields ``None`` so the facade falls back to an
empty extract; a provider never raises.
"""

from __future__ import annotations

from typing import Optional

from mote.common.logs import logger
from mote.context.code_map import ts_runtime
from mote.context.code_map.generic_builder import TreeSitterBuilder, module_summary
from mote.context.code_map.providers.base import ModuleResolver, ProviderExtract
from mote.context.code_map.providers.config import LangConfig


class _NullResolver:
    """A resolver that resolves nothing — for a language with no module⇄file map.

    Some languages (or a config that omits a resolver factory) have no
    file-anchored module scheme we can compute LSP-free; their in-file symbols and
    call graph still work, only cross-file import edges are dropped. Returning
    empty / ``None`` everywhere makes such a language contribute no dangling-import
    or reverse-dep edges rather than guessing (a wrong edge is worse than none).
    """

    def import_roots(self, abs_files: list[str]) -> set[str]:
        return set()

    def module_to_path(self, module: str, roots: set[str]) -> Optional[str]:
        return None

    def module_candidates(self, abspath: str) -> set[str]:
        return set()

    def is_relative(self, module: str) -> bool:
        return False


class TreeSitterProvider:
    """Parses one file of a tree-sitter language into a :class:`ProviderExtract`."""

    def __init__(self, config: LangConfig) -> None:
        self._config = config
        factory = config.module_resolver_factory
        self._resolver: ModuleResolver = factory() if factory is not None else _NullResolver()

    @property
    def language(self) -> str:
        return self._config.language

    @property
    def extensions(self) -> tuple[str, ...]:
        return self._config.extensions

    def module_resolver(self) -> ModuleResolver:
        return self._resolver

    def extract_tree(self, source: str, abspath: str) -> Optional[ProviderExtract]:
        """Parse *source* into the neutral extract, or ``None`` when unparseable."""
        try:
            root = ts_runtime.parse(self._config.ts_name, source)
            if root is None:
                return None
            builder = TreeSitterBuilder(self._config)
            graph, symbols = builder.build(root)
            imports, import_refs, import_bindings = self._extract_imports(root, source, abspath)
            return ProviderExtract(
                module_summary=module_summary(root, self._config.doc_comment),
                symbols=symbols,
                scope_graph=graph,
                imports=imports,
                import_refs=import_refs,
                import_bindings=import_bindings,
            )
        except Exception as exc:  # noqa: BLE001 — a parser/builder blow-up degrades to empty
            logger.debug(f"TreeSitterProvider[{self._config.language}]: extract of {abspath} failed: {exc}")
            return None

    def _extract_imports(self, root, source: str, abspath: str):
        """Run the config's import extractor best-effort ((), (), () on absence)."""
        extractor = self._config.import_extractor
        if extractor is None:
            return [], [], []
        try:
            return extractor(root, source.encode("utf-8"), abspath)
        except Exception as exc:  # noqa: BLE001 — imports are advisory, never fatal
            logger.debug(f"TreeSitterProvider[{self._config.language}]: import extract failed: {exc}")
            return [], [], []


__all__ = ["TreeSitterProvider"]
