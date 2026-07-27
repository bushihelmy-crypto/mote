"""Capture, fingerprint, and reapply security-relevant file metadata."""

from __future__ import annotations

import errno
import os
import stat
from typing import Optional

from mote.contracts.fileops.errors import MetadataPreservationError
from mote.runtime.fileops.identity import PathLike, native_path
from mote.runtime.fileops.metadata_manifest import PreservedMetadata

_NO_XATTR_ERRNOS = {getattr(errno, "ENOTSUP", -1), getattr(errno, "EOPNOTSUPP", -1)}


def capture_metadata(path: PathLike, metadata: Optional[os.stat_result] = None) -> PreservedMetadata:
    native = native_path(path)
    info = metadata or os.stat(native)
    xattrs: list[tuple[str, bytes]] = []
    xattrs_supported = hasattr(os, "listxattr") and hasattr(os, "getxattr")
    if xattrs_supported:
        try:
            names = os.listxattr(native, follow_symlinks=True)
            for name in sorted(names):
                xattrs.append((name, os.getxattr(native, name, follow_symlinks=True)))
        except OSError as exc:
            if exc.errno in _NO_XATTR_ERRNOS:
                xattrs_supported = False
                xattrs = []
            else:
                raise MetadataPreservationError(
                    f"cannot read file metadata for {os.fsdecode(native)}",
                    path=os.fsdecode(native),
                    cause=exc,
                ) from exc
    return PreservedMetadata(
        mode=stat.S_IMODE(info.st_mode),
        uid=getattr(info, "st_uid", None),
        gid=getattr(info, "st_gid", None),
        xattrs=tuple(xattrs),
        xattrs_supported=xattrs_supported,
    )


def apply_metadata(fd: int, path: PathLike, metadata: PreservedMetadata) -> None:
    native = native_path(path)
    try:
        if os.name == "posix" and metadata.uid is not None and metadata.gid is not None:
            os.fchown(fd, metadata.uid, metadata.gid)
        os.fchmod(fd, metadata.mode)
        if metadata.xattrs_supported:
            if not hasattr(os, "setxattr"):
                raise MetadataPreservationError("platform cannot restore extended attributes")
            for name, value in metadata.xattrs:
                os.setxattr(native, name, value, follow_symlinks=False)
    except MetadataPreservationError:
        raise
    except OSError as exc:
        raise MetadataPreservationError(
            f"cannot preserve metadata on temporary file {os.fsdecode(native)}",
            path=os.fsdecode(native),
            cause=exc,
        ) from exc

    actual = capture_metadata(native)
    identity_matches = (metadata.uid is None or actual.uid == metadata.uid) and (
        metadata.gid is None or actual.gid == metadata.gid
    )
    if (
        actual.mode != metadata.mode
        or actual.xattrs != metadata.xattrs
        or actual.xattrs_supported != metadata.xattrs_supported
        or not identity_matches
    ):
        raise MetadataPreservationError(
            f"metadata verification failed for temporary file {os.fsdecode(native)}",
            path=os.fsdecode(native),
            expected=metadata.digest,
            actual=actual.digest,
        )


__all__ = ["PreservedMetadata", "apply_metadata", "capture_metadata"]
