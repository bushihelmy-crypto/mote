#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The single optional-``rich`` import point for the builders package.

``rich`` is optional — every builder is guarded by ``_HAS_RICH`` and the
consumers degrade gracefully when it is absent (§9.10). Rather than repeat the
``try/except`` in each builder submodule, they all import the rich symbols they
need from here. When ``rich`` is unavailable the names resolve to ``None`` so
the submodules still *import* cleanly (a builder is only ever *called* behind a
``_HAS_RICH`` guard, so it never dereferences the ``None`` placeholders).
"""

from __future__ import annotations

try:  # rich is optional; the terminal degrades to plain text when absent.
    from rich import box
    from rich.padding import Padding
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text

    _HAS_RICH = True
except ImportError:  # pragma: no cover — exercised via the plain-text fallback
    box = None
    Padding = None
    Syntax = None
    Table = None
    Text = None
    _HAS_RICH = False


__all__ = ["box", "Padding", "Syntax", "Table", "Text", "_HAS_RICH"]
