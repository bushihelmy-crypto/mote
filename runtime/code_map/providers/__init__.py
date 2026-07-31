"""Provider assembly for live Code Map language providers.

:func:`all_providers` returns every :class:`~mote.runtime.code_map.providers.base.LanguageProvider`
that should back the code map in this environment. Python (stdlib ``ast``) is
*always* present; the tree-sitter-backed languages are appended only when the
tree-sitter runtime is importable (added in a later step), so a box without the
native grammars degrades cleanly to Python-only. :mod:`mote.runtime.code_map.languages`
turns this list into the extension→provider lookup the facade/indexer consume.
"""

from __future__ import annotations

from mote.runtime.code_map import ts_runtime
from mote.runtime.code_map._langconfigs import all_configs
from mote.runtime.code_map.providers.base import LanguageProvider, ModuleResolver, ProviderExtract
from mote.runtime.code_map.providers.python import PythonProvider
from mote.runtime.code_map.providers.tree_sitter import TreeSitterProvider


def all_providers() -> list[LanguageProvider]:
    """Every language provider active in this environment (Python always first).

    Python (stdlib ``ast``) is unconditional. Each declarative tree-sitter config
    is wrapped into a provider only when its grammar is loadable — the runtime is
    import-guarded, so a box without ``tree-sitter-language-pack`` (or a grammar
    that fails to build) simply contributes no provider and the map stays
    Python-only. A grammar the runtime cannot load is skipped, not fatal.
    """
    providers: list[LanguageProvider] = [PythonProvider()]
    if ts_runtime.available():
        for config in all_configs():
            if ts_runtime.has_grammar(config.ts_name):
                providers.append(TreeSitterProvider(config))
    return providers


__all__ = [
    "all_providers",
    "LanguageProvider",
    "ModuleResolver",
    "ProviderExtract",
    "PythonProvider",
]
