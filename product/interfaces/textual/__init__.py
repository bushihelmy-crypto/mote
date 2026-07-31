#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``mote.product.interfaces.textual`` — the full-screen Textual TUI host.

A *dedicated* terminal interface built on a full-screen Textual ``App``: a
scrolling transcript (``VerticalScroll``), a persistent :class:`StatusBar`, an
orange-``❯`` :class:`PromptInput`, and modal :class:`QuestionScreen` /
:class:`ApprovalScreen` overlays. It reuses the exact same presentation stack as
the rich terminal — **output** via a :class:`TextualConsumer` fed the shared
``ViewEvent`` protocol, **input** via a :class:`TextualPort`
(:class:`~mote.product.interaction.ports.InteractivePort`) — so the
``SessionDriver`` / ``ViewProjector`` / ``mote`` spine stay untouched.

``textual`` is an **optional** dependency. Importing this package never fails on
its absence: callers inspect ``_HAS_TEXTUAL`` before importing the concrete host
entry point. Concrete symbols live in their defining modules rather than behind
a dynamic package facade.
"""

from __future__ import annotations

try:  # textual is optional; the app degrades to the rich/plain hosts when absent.
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover — exercised only in a textual-less env
    _HAS_TEXTUAL = False

__all__ = ["_HAS_TEXTUAL"]
