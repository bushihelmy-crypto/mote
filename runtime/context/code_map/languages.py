"""Extension → provider registry — the language-dispatch index for the code map.

This is the language-agnostic lookup the extractor facade and the indexer route
through: which :class:`~mote.runtime.context.code_map.providers.base.LanguageProvider`
(if any) owns a given file, and the full set of extensions worth walking. The
registry is built once from :func:`~mote.runtime.context.code_map.providers.all_providers`
— Python always, tree-sitter languages when their runtime is present — so the
whole rest of the map never names a concrete language.

Absent the tree-sitter grammars, :func:`registered_extensions` is exactly
``{".py"}`` and :func:`provider_for` resolves only ``.py`` files, keeping the
Python path byte-for-byte identical to before the provider seam existed.
"""

from __future__ import annotations

import os
from typing import Optional

from mote.runtime.context.code_map.providers import all_providers
from mote.runtime.context.code_map.providers.base import LanguageProvider

# Extension (incl. leading dot) -> the provider that claims it. Built once at
# import time from the live provider set; a duplicate extension is a provider
# bug (first registration wins, deterministic by all_providers order).
_BY_EXTENSION: dict[str, LanguageProvider] = {}
_PROVIDERS: list[LanguageProvider] = []


def _build() -> None:
    _BY_EXTENSION.clear()
    _PROVIDERS.clear()
    for provider in all_providers():
        _PROVIDERS.append(provider)
        for ext in provider.extensions:
            _BY_EXTENSION.setdefault(ext, provider)


_build()


def provider_for(abspath: str) -> Optional[LanguageProvider]:
    """The provider claiming *abspath*'s extension, or ``None`` (unknown language)."""
    return _BY_EXTENSION.get(os.path.splitext(abspath)[1])


def registered_extensions() -> set[str]:
    """Every extension some provider claims — the indexer's walk filter."""
    return set(_BY_EXTENSION)


def all_registered_providers() -> list[LanguageProvider]:
    """The live provider list (Python always; tree-sitter langs when available)."""
    return list(_PROVIDERS)


__all__ = ["provider_for", "registered_extensions", "all_registered_providers"]
