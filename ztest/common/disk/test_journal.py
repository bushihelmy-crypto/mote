#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the L2 :class:`Journal` — a line-level append-only log.

Run synchronously: with no event loop the backing DiskWriter takes its
sync-fallback and writes inline, so an ``append_line`` is on disk by the time
``iter_raw_lines`` scans it (no drain needed, no singleton state leaks).

Covered: append/scan roundtrip, ``create_if_absent`` writes the header once and
is idempotent (resume/restart never re-writes it), blank lines are skipped, and a
missing journal scans as empty.
"""
from __future__ import annotations

from mote.runtime.disk.journal import Journal


def test_append_and_iter_roundtrip(tmp_path):
    j = Journal(tmp_path / "j.log")
    j.append_line("first")
    j.append_line("second")
    assert list(j.iter_raw_lines()) == ["first", "second"]


def test_create_if_absent_writes_header_once(tmp_path):
    j = Journal(tmp_path / "j.log")
    assert j.create_if_absent("header") is True
    assert list(j.iter_raw_lines()) == ["header"]


def test_create_if_absent_is_idempotent(tmp_path):
    path = tmp_path / "j.log"
    j = Journal(path)
    j.create_if_absent("header")
    j.append_line("body")
    # A second create on the existing file no-ops (does not re-write / truncate).
    assert j.create_if_absent("DIFFERENT-header") is False
    assert list(j.iter_raw_lines()) == ["header", "body"]


def test_iter_skips_blank_lines(tmp_path):
    path = tmp_path / "j.log"
    path.write_text("alpha\n\n\nbeta\n")
    assert list(Journal(path).iter_raw_lines()) == ["alpha", "beta"]


def test_iter_missing_file_is_empty(tmp_path):
    assert list(Journal(tmp_path / "absent.log").iter_raw_lines()) == []


def test_exists_reflects_file_state(tmp_path):
    j = Journal(tmp_path / "j.log")
    assert j.exists() is False
    j.append_line("x")
    assert j.exists() is True


def test_append_creates_missing_parent_dirs(tmp_path):
    j = Journal(tmp_path / "nested" / "deep" / "j.log")
    j.append_line("line")
    assert list(j.iter_raw_lines()) == ["line"]
