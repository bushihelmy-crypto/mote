from __future__ import annotations

import hashlib
import os
import stat

import pytest

from mote.contracts.file import AbsentVersion, MetadataPreservationError, SnapshotDurabilityError
from mote.contracts.file.errors import UnsupportedFilesystemSemanticsError
from mote.runtime.fileops.identity import name_identity, present_version
from mote.runtime.fileops.metadata import capture_metadata
from mote.runtime.fileops.metadata_manifest import PreservedMetadata, encode_metadata_manifest
from mote.runtime.fileops.publisher import AtomicPublisher
from mote.ztest.fileops_factory import FileMutationArtifactRepository


def _publisher(tmp_path):
    store = FileMutationArtifactRepository(
        tmp_path / "artifacts",
        hard_limit_bytes=1024 * 1024,
    )
    return AtomicPublisher(store), store


def _put(store, content, *, owner="publisher-test"):
    reservation = store.reserve(len(content), owner, 60)
    stage = store.stage(reservation, len(content))
    artifact = store.put(stage, (content,))
    store.release(reservation)
    return artifact


def _expected(path, store):
    if not os.path.lexists(path):
        return AbsentVersion(name_identity=name_identity(path))
    metadata = _put(
        store,
        encode_metadata_manifest(capture_metadata(path)),
        owner="publisher-test:expected-metadata",
    )
    content = path.read_bytes()
    return present_version(
        path,
        os.stat(path),
        digest=hashlib.sha256(content).hexdigest(),
        metadata_digest=metadata.digest,
    )


def _metadata(path, store):
    if not os.path.lexists(path):
        payload = encode_metadata_manifest(PreservedMetadata.for_create())
    else:
        payload = encode_metadata_manifest(capture_metadata(path))
    return _put(
        store,
        payload,
        owner="publisher-test:metadata",
    )


def test_replace_preserves_mode_and_publishes_complete_blob(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"old")
    target.chmod(0o640)
    publisher, store = _publisher(tmp_path)
    ref = _put(store, b"new content")

    publisher.replace_from_blob(
        target,
        ref,
        metadata=_metadata(target, store),
        expected=_expected(target, store),
    )

    assert target.read_bytes() == b"new content"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert list(tmp_path.glob(".target.txt.mote-*.tmp")) == []


def test_create_uses_private_default_mode(tmp_path):
    target = tmp_path / "new.txt"
    publisher, store = _publisher(tmp_path)

    publisher.replace_from_blob(
        target,
        _put(store, b"created"),
        metadata=_metadata(target, store),
        expected=_expected(target, store),
    )

    assert target.read_bytes() == b"created"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_replace_preserves_user_xattr_when_supported(tmp_path):
    if not all(hasattr(os, name) for name in ("setxattr", "getxattr")):
        pytest.skip("xattrs unavailable")
    target = tmp_path / "target.txt"
    target.write_bytes(b"old")
    try:
        os.setxattr(target, "user.mote-test", b"kept")
    except OSError:
        pytest.skip("filesystem does not support user xattrs")
    publisher, store = _publisher(tmp_path)

    publisher.replace_from_blob(
        target,
        _put(store, b"new"),
        metadata=_metadata(target, store),
        expected=_expected(target, store),
    )

    assert os.getxattr(target, "user.mote-test") == b"kept"


def test_hardlink_is_rejected_without_changing_either_name(tmp_path):
    target = tmp_path / "target.txt"
    alias = tmp_path / "alias.txt"
    target.write_bytes(b"old")
    os.link(target, alias)
    publisher, store = _publisher(tmp_path)

    with pytest.raises(UnsupportedFilesystemSemanticsError):
        publisher.replace_from_blob(
            target,
            _put(store, b"new"),
            metadata=_metadata(target, store),
            expected=_expected(target, store),
        )

    assert target.read_bytes() == b"old"
    assert alias.read_bytes() == b"old"


def test_publisher_refuses_unresolved_symlink(tmp_path):
    target = tmp_path / "target.txt"
    alias = tmp_path / "alias.txt"
    target.write_bytes(b"old")
    alias.symlink_to(target)
    publisher, store = _publisher(tmp_path)

    with pytest.raises(UnsupportedFilesystemSemanticsError):
        publisher.replace_from_blob(
            alias,
            _put(store, b"new"),
            metadata=_metadata(alias, store),
            expected=_expected(alias, store),
        )

    assert alias.is_symlink()
    assert target.read_bytes() == b"old"


def test_corrupt_artifact_never_reaches_target(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"old")
    publisher, store = _publisher(tmp_path)
    ref = _put(store, b"trusted")
    (store.root / ref.digest[:2] / ref.digest).write_bytes(b"corrupt")

    with pytest.raises(SnapshotDurabilityError):
        publisher.replace_from_blob(
            target,
            ref,
            metadata=_metadata(target, store),
            expected=_expected(target, store),
        )

    assert target.read_bytes() == b"old"


def test_metadata_failure_cleans_temp_and_keeps_target(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_bytes(b"old")
    publisher, store = _publisher(tmp_path)

    def fail_metadata(*args, **kwargs):
        raise MetadataPreservationError("injected")

    monkeypatch.setattr("mote.runtime.fileops.publisher.apply_metadata", fail_metadata)

    with pytest.raises(MetadataPreservationError):
        publisher.replace_from_blob(
            target,
            _put(store, b"new"),
            metadata=_metadata(target, store),
            expected=_expected(target, store),
        )

    assert target.read_bytes() == b"old"
    assert list(tmp_path.glob(".target.txt.mote-*.tmp")) == []


def test_delete_renames_to_private_tombstone_until_cleanup(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"old")
    publisher, store = _publisher(tmp_path)

    tombstone = publisher.delete(
        target,
        expected=_expected(target, store),
        transaction_id="tx1",
    )

    assert not target.exists()
    assert os.path.exists(tombstone)
    assert open(tombstone, "rb").read() == b"old"
    publisher.cleanup_tombstone(tombstone)
    assert not os.path.exists(tombstone)
