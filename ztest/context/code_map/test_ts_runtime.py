"""Tests for the tree-sitter runtime loader (import-guarded, thread-local cache)."""

from __future__ import annotations

import pytest

from mote.context.code_map import ts_runtime

pytest.importorskip("tree_sitter_language_pack")


def test_available_true_when_pack_present():
    assert ts_runtime.available() is True


def test_has_grammar_known_and_unknown():
    assert ts_runtime.has_grammar("javascript") is True
    assert ts_runtime.has_grammar("no_such_grammar_xyz") is False


def test_parser_for_caches_per_thread():
    a = ts_runtime.parser_for("javascript")
    b = ts_runtime.parser_for("javascript")
    assert a is not None
    assert a is b  # same thread → same cached parser instance


def test_parser_for_unknown_returns_none():
    assert ts_runtime.parser_for("definitely_not_a_grammar") is None


def test_parse_returns_root_node():
    root = ts_runtime.parse("javascript", "const x = 1;\n")
    assert root is not None
    assert root.type == "program"


def test_parse_unknown_grammar_none():
    assert ts_runtime.parse("nope_grammar", "x") is None
