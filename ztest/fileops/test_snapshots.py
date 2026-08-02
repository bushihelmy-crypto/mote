from __future__ import annotations

import hashlib
import os

import pytest

from mote.contracts.file import ContentChangedError, IdentityChangedError, SnapshotDurabilityError
from mote.runtime.fileops.resource_limits import ARTIFACT_HARD_LIMIT_BYTES, snapshot_budget
from mote.runtime.fileops.snapshots import SealedSnapshotReader
from mote.ztest.fileops_factory import FileMutationArtifactRepository


def _reader(tmp_path):
    store = FileMutationArtifactRepository(
        tmp_path / "artifacts",
        hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES,
    )
    return SealedSnapshotReader(store), store


def _open(reader, store, target, *, project_root):
    with store.write_scope(
        owner="test-snapshot",
        maximum_bytes=snapshot_budget(target.stat().st_size),
        ttl_seconds=60,
    ) as scope:
        snapshot = reader.open_snapshot(
            target,
            scope=scope,
            project_root=project_root,
        )
        scope.discard()
        return snapshot


def test_snapshot_is_sealed_and_content_addressed(tmp_path):
    target = tmp_path / "data.txt"
    target.write_bytes(b"hello\n")
    reader, store = _reader(tmp_path)

    snapshot = _open(reader, store, target, project_root=tmp_path)

    assert snapshot.version.digest == hashlib.sha256(b"hello\n").hexdigest()
    assert snapshot.version.size == 6
    assert store.read_bytes(snapshot.artifact) == b"hello\n"
    assert list((tmp_path / "artifacts-lifecycle" / ".incoming").iterdir()) == []


def test_snapshot_follows_symlink_but_preserves_requested_path(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    target = tmp_path / "target.txt"
    alias = tmp_path / "alias.txt"
    target.write_bytes(b"value")
    alias.symlink_to(target)
    reader, store = _reader(tmp_path)

    snapshot = _open(reader, store, alias, project_root=tmp_path)

    assert snapshot.requested_path.display == str(alias)
    assert snapshot.target_path.display == str(target)


def test_second_pass_detects_same_size_same_mtime_mutation(tmp_path):
    target = tmp_path / "data.txt"
    target.write_bytes(b"before")
    original_mtime = target.stat().st_mtime_ns
    reader, store = _reader(tmp_path)
    original_second_pass = reader._hash_second_pass

    def mutate_before_second_pass(source_fd):
        target.write_bytes(b"after!")
        os.utime(target, ns=(original_mtime, original_mtime))
        return original_second_pass(source_fd)

    reader._hash_second_pass = mutate_before_second_pass

    with pytest.raises(ContentChangedError):
        _open(reader, store, target, project_root=tmp_path)


def test_path_replacement_during_read_is_detected(tmp_path):
    target = tmp_path / "data.txt"
    replacement = tmp_path / "replacement.txt"
    target.write_bytes(b"same")
    replacement.write_bytes(b"same")
    reader, store = _reader(tmp_path)
    original_second_pass = reader._hash_second_pass

    def replace_before_second_pass(source_fd):
        os.replace(replacement, target)
        return original_second_pass(source_fd)

    reader._hash_second_pass = replace_before_second_pass

    with pytest.raises(IdentityChangedError):
        _open(reader, store, target, project_root=tmp_path)


def test_artifact_corruption_is_never_returned(tmp_path):
    reader, store = _reader(tmp_path)
    with store.write_scope(
        owner="test-corruption",
        maximum_bytes=len(b"trusted"),
        ttl_seconds=60,
    ) as scope:
        ref = scope.put_bytes(b"trusted")
        scope.discard()
    (store.root / ref.digest[:2] / ref.digest).write_bytes(b"corrupt")

    with pytest.raises(SnapshotDurabilityError):
        store.read_bytes(ref)


def test_text_view_uses_artifact_after_source_changes(tmp_path):
    target = tmp_path / "legacy.txt"
    target.write_bytes("原文".encode("gbk"))
    reader, store = _reader(tmp_path)
    snapshot = _open(reader, store, target, project_root=tmp_path)
    target.write_bytes("新文".encode("gbk"))

    text, editable = reader.read_text(snapshot, encoding="gbk")

    assert text == "原文"
    assert editable.logical_to_raw_boundaries[-1] == len("原文".encode("gbk"))
