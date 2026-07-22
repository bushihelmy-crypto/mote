#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the pluggable snapshot backend (git object db) + auto-detection.

Covers :class:`GitBlobStore` (put/get/exists/dedup via git plumbing, in an
independent bare repo), the ``detect_blob_backend`` heuristic (reusing
``git_state.find_git_root``), the ``make_blob_store`` factory, that the chosen
backend is stamped on the event, and that ``history`` diff/restore round-trips
through whichever backend recorded the before-image.

Tests that need the ``git`` binary are skipped when it is unavailable.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from mote.session.events import FILE_SNAPSHOT
from mote.session.history import diff_snapshot, file_history, restore
from mote.session.log import SessionLog
from mote.session.snapshot import BlobStore, FileSnapshotRecorder, GitBlobStore, detect_blob_backend, make_blob_store

git_required = pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available")


# ---------------------------------------------------------------------------
# make_blob_store factory
# ---------------------------------------------------------------------------


def test_make_blob_store_selects_backend(tmp_path):
    assert isinstance(make_blob_store(tmp_path, "blob"), BlobStore)
    assert isinstance(make_blob_store(tmp_path, "git"), GitBlobStore)
    # Unknown / default falls back to the plain blob store.
    assert isinstance(make_blob_store(tmp_path), BlobStore)
    assert isinstance(make_blob_store(tmp_path, "nonsense"), BlobStore)


def test_store_name_tags(tmp_path):
    assert BlobStore(tmp_path).name == "blob"
    assert GitBlobStore(tmp_path).name == "git"


# ---------------------------------------------------------------------------
# GitBlobStore (git object db backend)
# ---------------------------------------------------------------------------


@git_required
def test_gitblobstore_put_get_roundtrips(tmp_path):
    store = GitBlobStore(tmp_path)
    content = b"hello git blob"
    digest = store.put(content)
    # git blob ids are 40-char sha1 hex.
    assert len(digest) == 40
    assert store.exists(digest)
    assert store.get(digest) == content


@git_required
def test_gitblobstore_dedups_identical_content(tmp_path):
    store = GitBlobStore(tmp_path)
    d1 = store.put(b"same")
    d2 = store.put(b"same")
    assert d1 == d2


@git_required
def test_gitblobstore_get_missing_returns_none(tmp_path):
    store = GitBlobStore(tmp_path)
    store.put(b"seed")  # ensure repo exists
    assert store.get("0" * 40) is None


@git_required
def test_gitblobstore_exists_false_before_repo(tmp_path):
    store = GitBlobStore(tmp_path)
    # No put yet -> no repo on disk -> exists/get degrade gracefully.
    assert store.exists("0" * 40) is False
    assert store.get("0" * 40) is None


@git_required
def test_gitblobstore_uses_independent_bare_repo(tmp_path):
    store = GitBlobStore(tmp_path)
    store.put(b"x")
    # An independent bare repo lives under {base}/git, not the user's repo.
    assert (tmp_path / "git" / "HEAD").exists()


# ---------------------------------------------------------------------------
# detect_blob_backend
# ---------------------------------------------------------------------------


@git_required
def test_detect_backend_inside_repo_is_git(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    assert detect_blob_backend(str(tmp_path)) == "git"


@git_required
def test_detect_backend_in_subdir_of_repo_is_git(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert detect_blob_backend(str(sub)) == "git"


def test_detect_backend_no_git_binary_is_blob(tmp_path, monkeypatch):
    monkeypatch.setattr("mote.session.snapshot.shutil.which", lambda _: None)
    # Even inside a repo, absence of the binary forces the blob backend.
    assert detect_blob_backend(str(tmp_path)) == "blob"


# ---------------------------------------------------------------------------
# Recorder stamps the backend on the event
# ---------------------------------------------------------------------------


def test_recorder_records_blob_backend_by_default(tmp_path):
    target = tmp_path / "f.txt"
    target.write_bytes(b"data")
    log = SessionLog("be_blob", base_dir=str(tmp_path))
    rec = FileSnapshotRecorder(log)  # default backend
    rec.snapshot(str(target), tool="Write")

    payload = list(log.iter_raw())[0]["payload"]
    assert payload["backend"] == "blob"


@git_required
def test_recorder_records_git_backend(tmp_path):
    target = tmp_path / "f.txt"
    target.write_bytes(b"data")
    log = SessionLog("be_git", base_dir=str(tmp_path))
    rec = FileSnapshotRecorder(log, backend="git")
    rec.snapshot(str(target), tool="Write")

    record = list(log.iter_raw())[0]
    assert record["type"] == FILE_SNAPSHOT
    assert record["payload"]["backend"] == "git"
    # The before-image is fetchable from the git store by its blob id.
    assert rec.blobs.get(record["payload"]["pre_hash"]) == b"data"


# ---------------------------------------------------------------------------
# history diff/restore round-trips through the recorded backend
# ---------------------------------------------------------------------------


@git_required
def test_history_restore_with_git_backend(tmp_path):
    target = tmp_path / "f.txt"
    target.write_bytes(b"v1\n")
    log = SessionLog("hist_git", base_dir=str(tmp_path))
    rec = FileSnapshotRecorder(log, backend="git")
    rec.snapshot(str(target), tool="Edit")

    # Mutate on disk, then restore from the git-stored before-image.
    target.write_bytes(b"v2\n")
    entries = file_history(log)[str(target)]
    assert entries[0].backend == "git"

    diff = diff_snapshot(log, str(target))
    assert "v1" in diff and "v2" in diff

    assert restore(log, str(target)) is True
    assert target.read_bytes() == b"v1\n"


def test_history_default_backend_is_blob(tmp_path):
    target = tmp_path / "f.txt"
    target.write_bytes(b"hello\n")
    log = SessionLog("hist_blob", base_dir=str(tmp_path))
    rec = FileSnapshotRecorder(log)
    rec.snapshot(str(target), tool="Edit")

    entries = file_history(log)[str(target)]
    assert entries[0].backend == "blob"

    target.write_bytes(b"changed\n")
    assert restore(log, str(target)) is True
    assert target.read_bytes() == b"hello\n"
