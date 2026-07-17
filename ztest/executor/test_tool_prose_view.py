#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Generated prose view over every registered tool + the two-level invariant.

Replaces the old hand-maintained ``common/prompt/tools.py`` description
warehouse: instead of a parallel file to keep in sync, the single source of
truth is each tool's ``call()`` docstring, and this module *derives* the review
view from it. It walks the live registry and asserts the docstring-native
contract holds for the whole toolbox:

- every tool exposes a non-empty one-line :meth:`~BaseTool.summary` (the menu
  entry — the docstring's first line, or a custom_schema description's first
  line for a dynamic tool);
- the summary is genuinely one line;
- the wire ``description`` starts with that same summary (the menu and the full
  description share their opening sentence — author once);
- the wire ``description`` never leaks the ``Args:`` block (params travel
  separately as schema, not duplicated as prose).

``build_prose_view()`` renders the same data as a human-readable catalogue for
eyeballing — the "generated view" the design calls for.
"""
from __future__ import annotations

import inspect

import pytest

from mote.common.utils.docstring import first_line
from mote.executor.tool_registry import registry


def _all_tool_classes() -> list[type]:
    """Every registered tool class (deduped by primary name), discovery forced."""
    registry.discover()
    return sorted(registry.all_tools().values(), key=lambda c: c.name)


def build_prose_view() -> str:
    """Render every tool's two-level prose as one reviewable catalogue string.

    ``NAME — summary`` header per tool, then its full wire description indented.
    Not asserted on; a convenience dump for humans (and doctest-free by intent).
    """
    blocks: list[str] = []
    for cls in _all_tool_classes():
        summary = cls.summary()
        desc = cls.get_schema().get("description", "")
        body = "\n".join(f"    {line}" for line in desc.splitlines())
        blocks.append(f"{cls.name} — {summary}\n{body}")
    return "\n\n".join(blocks)


_TOOL_CLASSES = _all_tool_classes()
_IDS = [c.name for c in _TOOL_CLASSES]


class TestTwoLevelProseInvariant:
    """Every registered tool honours the docstring-native two-level contract."""

    def test_registry_is_non_empty(self):
        # Guard the parametrization itself: discovery must find real tools.
        assert _TOOL_CLASSES, "no tools discovered — registry scan is broken"

    @pytest.mark.parametrize("cls", _TOOL_CLASSES, ids=_IDS)
    def test_summary_is_non_empty(self, cls):
        assert cls.summary().strip(), f"{cls.name} has no one-line summary"

    @pytest.mark.parametrize("cls", _TOOL_CLASSES, ids=_IDS)
    def test_summary_is_single_line(self, cls):
        assert "\n" not in cls.summary(), f"{cls.name} summary spans multiple lines"

    @pytest.mark.parametrize("cls", _TOOL_CLASSES, ids=_IDS)
    def test_description_opens_with_summary(self, cls):
        # Menu blurb and full description share their opening sentence — the
        # first line of the wire description IS the summary.
        desc = cls.get_schema().get("description", "")
        assert desc, f"{cls.name} has an empty wire description"
        assert first_line(desc) == cls.summary(), f"{cls.name}: description first line diverges from summary()"

    @pytest.mark.parametrize("cls", _TOOL_CLASSES, ids=_IDS)
    def test_description_does_not_leak_args_block(self, cls):
        # Params are schema, not prose: the ``Args:`` section must be stripped
        # from the wire description. A custom_schema tool is exempt (it owns its
        # description wholesale and may format params however it likes).
        if cls.custom_schema() is not None:
            return
        desc = cls.get_schema().get("description", "")
        for line in desc.splitlines():
            header = line.strip().rstrip(":").lower()
            assert header != "args", f"{cls.name} leaks an Args: block into the description"

    @pytest.mark.parametrize("cls", _TOOL_CLASSES, ids=_IDS)
    def test_summary_matches_docstring_first_line_when_auto(self, cls):
        # For an auto-schema tool (no custom_schema), summary() must be exactly
        # the call() docstring's first line — no drift.
        if cls.custom_schema() is not None:
            return
        assert cls.summary() == first_line(inspect.getdoc(cls.call))


def test_build_prose_view_covers_every_tool():
    view = build_prose_view()
    for cls in _TOOL_CLASSES:
        assert cls.name in view, f"{cls.name} missing from the generated prose view"
