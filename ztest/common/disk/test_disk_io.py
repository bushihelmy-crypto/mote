#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the L0 disk primitives: ``atomic_write`` and the ``write_bytes`` fsync flag.

``atomic_write`` is the single home for the ``tmp + fsync + replace`` pattern.
The contract a caller depends on: the destination is replaced wholesale (a reader
never sees a half-written file), no temp file is left behind, and an overwrite
fully swaps the contents. fsync is exercised for its side-effect (no crash); we
can't observe durability from a unit test, so we assert the bytes land.
"""
from __future__ import annotations

import os
from pathlib import Path

from mote.runtime.disk.disk_io import atomic_write, file_size, mtime_ns, mtime_seconds, write_bytes


def test_atomic_write_creates_file_with_contents(tmp_path):
    dest = tmp_path / "f.bin"
    atomic_write(dest, b"hello")
    assert dest.read_bytes() == b"hello"


def test_atomic_write_overwrites_existing(tmp_path):
    dest = tmp_path / "f.bin"
    dest.write_bytes(b"old-and-longer")
    atomic_write(dest, b"new")
    assert dest.read_bytes() == b"new"


def test_atomic_write_leaves_no_tmp_files(tmp_path):
    dest = tmp_path / "f.bin"
    atomic_write(dest, b"data")
    leftover = [p for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftover == []


def test_atomic_write_creates_missing_parent_dirs(tmp_path):
    dest = tmp_path / "a" / "b" / "f.bin"
    atomic_write(dest, b"deep")
    assert dest.read_bytes() == b"deep"


def test_atomic_write_without_fsync_still_writes(tmp_path):
    dest = tmp_path / "f.bin"
    atomic_write(dest, b"nofsync", fsync=False)
    assert dest.read_bytes() == b"nofsync"


def test_write_bytes_append_default(tmp_path):
    dest = tmp_path / "f.bin"
    write_bytes(dest, b"one")
    write_bytes(dest, b"two")
    assert dest.read_bytes() == b"onetwo"


def test_write_bytes_truncate(tmp_path):
    dest = tmp_path / "f.bin"
    dest.write_bytes(b"existing")
    write_bytes(dest, b"fresh", append=False)
    assert dest.read_bytes() == b"fresh"


def test_write_bytes_with_fsync_returns_length(tmp_path):
    dest = tmp_path / "f.bin"
    n = write_bytes(dest, b"durable", append=False, fsync=True)
    assert n == len(b"durable")
    assert dest.read_bytes() == b"durable"


def test_file_size_reports_byte_length(tmp_path):
    dest = tmp_path / "f.bin"
    dest.write_bytes(b"12345")
    assert file_size(dest) == 5


def test_file_size_missing_is_zero(tmp_path):
    assert file_size(tmp_path / "nope.bin") == 0


def test_mtime_ns_matches_stat(tmp_path):
    dest = tmp_path / "f.bin"
    dest.write_bytes(b"x")
    assert mtime_ns(dest) == os.stat(dest).st_mtime_ns


def test_mtime_ns_changes_after_touch(tmp_path):
    dest = tmp_path / "f.bin"
    dest.write_bytes(b"x")
    st = os.stat(dest)
    # Bump the mtime by 1ms so the same-tick write can't leave it unchanged.
    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert mtime_ns(dest) == st.st_mtime_ns + 1_000_000


def test_mtime_ns_missing_is_none(tmp_path):
    # A missing / unstat-able path yields None (treated as "changed"), not a raise.
    assert mtime_ns(tmp_path / "nope.bin") is None


def test_mtime_seconds_matches_stat(tmp_path):
    dest = tmp_path / "f.bin"
    dest.write_bytes(b"x")
    assert mtime_seconds(dest) == os.stat(dest).st_mtime


def test_mtime_seconds_missing_is_none(tmp_path):
    assert mtime_seconds(tmp_path / "nope.bin") is None
