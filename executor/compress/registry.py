"""Route a parsed command prefix to the compressor that claims it.

Matching is layered, most-specific first:

1. **Full prefix** — an exact / containing match against a compressor's
   declared prefixes (``"git log"`` -> :class:`GitCompressor`).
2. **Head token** — the command's first word (``"git"`` / ``"pytest"``)
   matches the first word of any declared prefix.
3. **``python -m <module>``** — a ``-m pytest`` / ``-m ruff`` invocation is
   routed by the module name.

Returns ``None`` when nothing claims the command.
"""

from __future__ import annotations

from typing import Optional

from metagpt.executor.compress.base import Compressor
from metagpt.executor.compress.cargo import CargoCompressor
from metagpt.executor.compress.git import GitCompressor
from metagpt.executor.compress.pip import PipCompressor
from metagpt.executor.compress.pytest import PytestCompressor
from metagpt.executor.compress.ruff import RuffCompressor

# Order is not significant: prefixes are disjoint across compressors.
_COMPRESSORS: tuple[Compressor, ...] = (
    GitCompressor(),
    PytestCompressor(),
    RuffCompressor(),
    CargoCompressor(),
    PipCompressor(),
)


def _dash_m_module(argv: list[str]) -> str:
    """Module name of a ``python -m <module>`` invocation, else ``""``."""
    for i, tok in enumerate(argv):
        if tok == "-m" and i + 1 < len(argv):
            return argv[i + 1]
    return ""


def lookup_compressor(prefix: Optional[str], argv: Optional[list[str]] = None) -> Optional[Compressor]:
    """Return the compressor that claims *prefix* / *argv*, or ``None``."""
    prefix = (prefix or "").strip()
    argv = argv or []

    # 1. Full-prefix routing (most specific).
    if prefix:
        for c in _COMPRESSORS:
            for p in c.prefixes:
                if prefix == p or prefix.startswith(p + " ") or p.startswith(prefix + " "):
                    return c

    # 2. Head-token routing (``git anything`` -> GitCompressor).
    head = prefix.split()[0] if prefix else ""
    if head:
        for c in _COMPRESSORS:
            for p in c.prefixes:
                if p.split()[0] == head:
                    return c

    # 3. ``python -m pytest`` / ``python -m ruff`` fallback.
    module = _dash_m_module(argv)
    if module:
        for c in _COMPRESSORS:
            for p in c.prefixes:
                if p.split()[0] == module:
                    return c

    return None
