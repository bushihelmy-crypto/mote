"""Tests for the extension → provider registry (language dispatch + degrade)."""

from __future__ import annotations

import pytest

from mote.runtime.context.code_map import languages

_HAS_TS = pytest.importorskip("tree_sitter_language_pack") is not None


def test_python_always_registered():
    prov = languages.provider_for("/repo/mod.py")
    assert prov is not None
    assert prov.language == "python"
    assert ".py" in languages.registered_extensions()


def test_unknown_extension_has_no_provider():
    assert languages.provider_for("/repo/data.unknownext") is None


def test_javascript_registered_when_runtime_present():
    prov = languages.provider_for("/repo/app.js")
    assert prov is not None
    assert prov.language == "javascript"
    exts = languages.registered_extensions()
    assert {".js", ".jsx", ".mjs", ".cjs"} <= exts


def test_registered_extensions_is_only_py_when_runtime_absent(monkeypatch):
    # Simulate an environment without the tree-sitter runtime and rebuild.
    from mote.runtime.context.code_map import ts_runtime

    monkeypatch.setattr(ts_runtime, "_AVAILABLE", False)
    languages._build()
    try:
        assert languages.registered_extensions() == {".py"}
        assert languages.provider_for("/repo/app.js") is None
    finally:
        # Undo the patch FIRST so _AVAILABLE is True again, THEN rebuild the real
        # registry (rebuilding under a still-patched flag would leak JS-less state).
        monkeypatch.undo()
        languages._build()
    assert ".js" in languages.registered_extensions()
