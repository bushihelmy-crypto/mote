#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.roles.lsp.format`` — diagnostics -> context block."""
from __future__ import annotations

from mote.roles.lsp.format import format_diagnostics
from mote.roles.lsp.registry import Diagnostic


def test_empty_is_blank():
    assert format_diagnostics({}) == ""


def test_renders_diagnostics():
    changed = {
        "a.py": [
            Diagnostic(
                severity=1, line=11, character=4, message="Undefined name 'foo'", source="pyflakes", code="F821"
            ),
        ]
    }
    out = format_diagnostics(changed)
    assert "<lsp_diagnostics>" in out
    assert "</lsp_diagnostics>" in out
    assert "a.py" in out
    # 0-based 11:4 surfaced 1-based as 12:5.
    assert "Error [12:5] Undefined name 'foo'" in out
    assert "[F821]" in out
    assert "(pyflakes)" in out


def test_resolved_file():
    out = format_diagnostics({"a.py": []})
    assert "a.py" in out
    assert "(no diagnostics — resolved)" in out


def test_multiple_files():
    changed = {
        "a.py": [Diagnostic(severity=2, line=0, character=0, message="warn")],
        "b.py": [Diagnostic(severity=1, line=1, character=1, message="err")],
    }
    out = format_diagnostics(changed)
    assert "a.py" in out and "b.py" in out
    assert "Warning" in out and "Error" in out
