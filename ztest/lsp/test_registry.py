#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.roles.lsp.registry`` — dedup + volume-limited diagnostics."""
from __future__ import annotations

from mote.roles.lsp.registry import Diagnostic, DiagnosticRegistry, parse_diagnostic, severity_label


def _d(line=0, msg="boom", sev=1):
    return Diagnostic(severity=sev, line=line, character=0, message=msg)


def test_publish_and_drain_once():
    reg = DiagnosticRegistry()
    reg.publish("a.py", [_d(1, "x"), _d(2, "y")])
    assert reg.has_changes() is True
    changed = reg.drain_changed()
    assert set(changed) == {"a.py"}
    assert len(changed["a.py"]) == 2
    # Drained — no longer a change until something differs.
    assert reg.has_changes() is False
    assert reg.drain_changed() == {}


def test_unchanged_set_not_redelivered():
    reg = DiagnosticRegistry()
    reg.publish("a.py", [_d(1, "x")])
    reg.drain_changed()
    # Republishing the identical set is not a change.
    reg.publish("a.py", [_d(1, "x")])
    assert reg.has_changes() is False


def test_changed_set_redelivered():
    reg = DiagnosticRegistry()
    reg.publish("a.py", [_d(1, "x")])
    reg.drain_changed()
    reg.publish("a.py", [_d(1, "x"), _d(2, "y")])
    assert reg.has_changes() is True
    changed = reg.drain_changed()
    assert len(changed["a.py"]) == 2


def test_cleared_file_reported_resolved():
    reg = DiagnosticRegistry()
    reg.publish("a.py", [_d(1, "x")])
    reg.drain_changed()
    # Server republishes empty -> file cleared.
    reg.publish("a.py", [])
    assert reg.has_changes() is True
    changed = reg.drain_changed()
    assert changed == {"a.py": []}
    # And not reported again.
    assert reg.has_changes() is False


def test_per_file_cap():
    reg = DiagnosticRegistry()
    reg.publish("a.py", [_d(i, f"m{i}") for i in range(25)])
    changed = reg.drain_changed()
    assert len(changed["a.py"]) == 10  # _MAX_PER_FILE


def test_total_cap_across_files():
    reg = DiagnosticRegistry()
    for f in range(5):
        reg.publish(f"f{f}.py", [_d(i, f"m{i}") for i in range(10)])
    changed = reg.drain_changed()
    total = sum(len(v) for v in changed.values())
    assert total <= 30  # _MAX_TOTAL


def test_cap_prioritizes_severity():
    reg = DiagnosticRegistry()
    diags = [_d(i, f"warn{i}", sev=2) for i in range(9)] + [_d(99, "the error", sev=1)]
    reg.publish("a.py", diags)
    changed = reg.drain_changed()
    # The single error must survive the cap (errors sort first).
    assert any(d.severity == 1 for d in changed["a.py"])


def test_parse_diagnostic_ok():
    raw = {
        "range": {"start": {"line": 4, "character": 2}, "end": {"line": 4, "character": 8}},
        "severity": 2,
        "message": "unused import",
        "source": "pyflakes",
        "code": "F401",
    }
    d = parse_diagnostic(raw)
    assert d is not None
    assert d.line == 4 and d.character == 2
    assert d.severity == 2
    assert d.message == "unused import"
    assert d.source == "pyflakes"
    assert d.code == "F401"


def test_parse_diagnostic_missing_message_skipped():
    assert parse_diagnostic({"range": {"start": {"line": 1}}}) is None
    assert parse_diagnostic("not a dict") is None


def test_parse_diagnostic_defaults():
    d = parse_diagnostic({"message": "x"})
    assert d is not None
    assert d.severity == 1 and d.line == 0 and d.character == 0


def test_severity_label():
    assert severity_label(1) == "Error"
    assert severity_label(2) == "Warning"
    assert severity_label(99) == "Diag"
