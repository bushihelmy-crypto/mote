#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pure-function tests for the browser's unified page-tree serializer.

These do not touch Playwright/Chromium (unlike ``test_web_browser.py``), so they
run everywhere. They exercise :func:`_format_tree` on hand-built node lists (the
shape :data:`_TREE_JS` returns from the page) and :func:`_ref_error` — the two
pure functions the unified ``snapshot`` is built on. The Playwright-gated tests
in ``test_web_browser.py`` cover the JS DOM walk end-to-end.
"""
from __future__ import annotations

from metagpt.executor.dependency._browser import _format_tree, _ref_error


def _text(depth: int, text: str) -> dict:
    return {"kind": "text", "depth": depth, "text": text}


def _el(depth: int, ref: str, tag: str, **kw) -> dict:
    node = {"kind": "element", "depth": depth, "ref": ref, "tag": tag}
    node.update(kw)
    return node


# ---------------------------------------------------------------------------
# _format_tree — interleave, indentation, filters, attr whitelist
# ---------------------------------------------------------------------------


def test_interleaves_text_and_elements_in_order():
    """Text and element nodes render in list order (DOM reading order)."""
    nodes = [
        _text(0, "Welcome to the shop"),
        _el(0, "1", "a", name="Home", href="/"),
        _text(0, "Find what you need"),
        _el(0, "2", "button", name="Buy now"),
    ]
    out = _format_tree(nodes, prev_refs={"1", "2"})
    lines = out.splitlines()
    assert lines[0] == "Welcome to the shop"
    assert lines[1] == '[1]<a href="/">Home'
    assert lines[2] == "Find what you need"
    assert lines[3] == "[2]<button>Buy now"


def test_indentation_tracks_depth():
    """Two spaces of indent per depth level, for both text and elements."""
    nodes = [
        _text(0, "top"),
        _el(1, "1", "button", name="nested"),
        _text(2, "deep"),
    ]
    out = _format_tree(nodes, prev_refs={"1"})
    lines = out.splitlines()
    assert lines[0] == "top"
    assert lines[1] == "  [1]<button>nested"
    assert lines[2] == "    deep"


def test_is_new_marker_for_refs_not_in_prev():
    """A leading * marks elements whose ref is not in prev_refs."""
    nodes = [
        _el(0, "1", "button", name="old"),
        _el(0, "2", "button", name="new"),
    ]
    out = _format_tree(nodes, prev_refs={"1"})
    lines = out.splitlines()
    assert lines[0] == "[1]<button>old"
    assert lines[1] == "*[2]<button>new"


def test_all_new_when_prev_refs_empty():
    """With no baseline (fresh page), every element is marked new."""
    nodes = [_el(0, "1", "a", name="Home", href="/")]
    out = _format_tree(nodes)  # prev_refs defaults to empty frozenset
    assert out == '*[1]<a href="/">Home'


def test_interactive_only_drops_text_nodes():
    """interactive_only=True emits only element lines (same tree, filtered)."""
    nodes = [
        _text(0, "prose one"),
        _el(0, "1", "button", name="Click"),
        _text(1, "prose two"),
        _el(1, "2", "input", type="text", placeholder="Search"),
    ]
    out = _format_tree(nodes, prev_refs={"1", "2"}, interactive_only=True)
    lines = out.splitlines()
    assert lines == ["[1]<button>Click", '  [2]<input type="text">Search']


def test_attr_whitelist_type_role_checked():
    """type/role/checked render; nothing else leaks onto the element line."""
    node = _el(0, "1", "input", type="checkbox", role="switch", checked=True, name="Notify")
    out = _format_tree([node], prev_refs={"1"})
    assert out == '[1]<input type="checkbox" role="switch" checked>Notify'


def test_href_truncated_over_80_chars():
    """A long href is truncated to keep a single link from blowing up a line."""
    long_href = "https://example.com/" + "a" * 100
    node = _el(0, "1", "a", name="Link", href=long_href)
    out = _format_tree([node], prev_refs={"1"})
    assert "..." in out
    # 77 chars + "..." per the _format_snapshot_line rule.
    assert long_href[:77] + "..." in out
    assert long_href not in out


def test_placeholder_fallback_when_no_name():
    """An input with no accessible name surfaces its placeholder as the label."""
    node = _el(0, "1", "input", type="text", name="", placeholder="Type here")
    out = _format_tree([node], prev_refs={"1"})
    assert out == '[1]<input type="text">Type here'


def test_empty_node_list_is_empty_string():
    assert _format_tree([]) == ""


def test_blank_text_nodes_skipped():
    """Whitespace-only text nodes produce no line."""
    nodes = [_text(0, "   "), _el(0, "1", "button", name="Go")]
    out = _format_tree(nodes, prev_refs={"1"})
    assert out == "[1]<button>Go"


# ---------------------------------------------------------------------------
# _ref_error — actionable "re-snapshot" wording
# ---------------------------------------------------------------------------


def test_ref_error_known_ref_says_page_changed():
    meta = {"5": {"tag": "button", "name": "Buy"}}
    msg = _ref_error("5", meta)
    assert "[5]" in msg
    assert "page changed since the last snapshot" in msg
    assert "fresh snapshot" in msg


def test_ref_error_unknown_ref_says_never_assigned():
    msg = _ref_error("999", {})
    assert "[999]" in msg
    assert "no element [999] in the last snapshot" in msg
    assert "fresh snapshot" in msg
