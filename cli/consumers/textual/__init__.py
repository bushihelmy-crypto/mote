#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``metagpt.cli.consumers.textual`` — the full-screen Textual TUI host.

A *dedicated* terminal interface built on a full-screen Textual ``App``: a
scrolling transcript (``VerticalScroll``), a persistent :class:`StatusBar`, an
orange-``❯`` :class:`PromptInput`, and modal :class:`QuestionScreen` /
:class:`ApprovalScreen` overlays. It reuses the exact same presentation stack as
the rich terminal — **output** via a :class:`TextualConsumer` fed the shared
``ViewEvent`` protocol, **input** via a :class:`TextualPort`
(:class:`~metagpt.cli.common.interface.ports.InteractivePort`) — so the
``SessionDriver`` / ``ViewProjector`` / ``metagpt`` spine stay untouched.

``textual`` is an **optional** dependency. Importing this package never fails on
its absence: the ``_HAS_TEXTUAL`` guard is checked and the host entry points are
exposed lazily via :pep:`562` ``__getattr__`` so a rich-only environment can
import the package (to discover the flag) without pulling in ``textual``.
"""

from __future__ import annotations

from typing import Any

try:  # textual is optional; the app degrades to the rich/plain hosts when absent.
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover — exercised only in a textual-less env
    _HAS_TEXTUAL = False


# Lazy exports (PEP 562): only import the textual-dependent host on first access
# so ``import metagpt.cli.consumers.textual`` is safe without ``textual`` present.
_LAZY = {
    "run_textual": ("metagpt.cli.consumers.textual.app", "run_textual"),
    "MetaGPTApp": ("metagpt.cli.consumers.textual.app", "MetaGPTApp"),
    "ViewEventMessage": ("metagpt.cli.consumers.textual.app", "ViewEventMessage"),
    "TextualConsumer": ("metagpt.cli.consumers.textual.consumer", "TextualConsumer"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    import importlib

    return getattr(importlib.import_module(module_name), attr)


__all__ = ["_HAS_TEXTUAL", "run_textual", "MetaGPTApp", "ViewEventMessage", "TextualConsumer"]
