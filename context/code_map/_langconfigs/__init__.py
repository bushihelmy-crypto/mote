"""Declarative language configs — the data each tree-sitter language contributes.

:func:`all_configs` is the single roster of every :class:`LangConfig` the
tree-sitter provider can back. :mod:`mote.context.code_map.providers` wraps each
into a :class:`~mote.context.code_map.providers.tree_sitter.TreeSitterProvider`
*iff* the tree-sitter runtime is importable, so absence of the native grammars
degrades cleanly to Python-only. Adding a language is adding one module here and
one line to this list — no engine change.
"""

from __future__ import annotations

from mote.context.code_map._langconfigs.cfamily import CPP, C
from mote.context.code_map._langconfigs.csharp import CSHARP
from mote.context.code_map._langconfigs.go import GO
from mote.context.code_map._langconfigs.java import JAVA
from mote.context.code_map._langconfigs.javascript import JAVASCRIPT
from mote.context.code_map._langconfigs.rust import RUST
from mote.context.code_map._langconfigs.typescript import TSX, TYPESCRIPT
from mote.context.code_map.providers.config import LangConfig


def all_configs() -> list[LangConfig]:
    """Every declarative language config, in registration order."""
    return [JAVASCRIPT, TYPESCRIPT, TSX, GO, RUST, JAVA, CSHARP, C, CPP]


__all__ = ["all_configs"]
