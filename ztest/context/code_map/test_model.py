"""Tests for the neutral extract model + its extractor re-exports."""

from __future__ import annotations

from mote.context.code_map.model import CallEdge, FileExtract, ImportBinding, ImportRef, Symbol


def test_fileextract_language_defaults_empty():
    fe = FileExtract(path="/x/y.py")
    assert fe.language == ""
    assert fe.symbols == []
    assert fe.scope_graph is None


def test_fileextract_language_settable():
    fe = FileExtract(path="/x/y.go", language="go")
    assert fe.language == "go"


def test_symbol_kind_is_free_form():
    s = Symbol(name="S", qualified_name="S", kind="struct", start_line=1)
    assert s.kind == "struct"


def test_extractor_re_exports_model_dataclasses():
    # ``from ...extractor import Symbol`` must keep working after the move.
    from mote.context.code_map import extractor as ex

    assert ex.Symbol is Symbol
    assert ex.CallEdge is CallEdge
    assert ex.ImportRef is ImportRef
    assert ex.ImportBinding is ImportBinding
    assert ex.FileExtract is FileExtract
