"""Filesystem and path identities used by snapshots, locks, and revisions."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Optional, Union

from mote.contracts.file.identity import (
    NameIdentity,
    NativePath,
    PathToken,
    PresentVersion,
    ProjectIdentity,
    TargetIdentity,
)

PathLike = Union[str, bytes, Path, PathToken]


def native_path(path: PathLike) -> NativePath:
    value = path.native if isinstance(path, PathToken) else os.fspath(path)
    if not isinstance(value, (str, bytes)):
        raise TypeError(f"unsupported path type: {type(value).__name__}")
    value = os.path.expanduser(value)
    return os.path.abspath(value)


def path_token(path: PathLike) -> PathToken:
    native = native_path(path)
    return PathToken(display=os.fsdecode(native), native=native)


def resolve_existing_target(path: PathLike) -> tuple[PathToken, PathToken]:
    requested = path_token(path)
    metadata = os.lstat(requested.native)
    if stat.S_ISLNK(metadata.st_mode):
        target_native = os.path.realpath(requested.native)
    else:
        target_native = requested.native
    target_metadata = os.stat(target_native)
    if not stat.S_ISREG(target_metadata.st_mode):
        raise OSError(f"not a regular file: {requested.display}")
    return requested, path_token(target_native)


def _digest(parts: list[bytes]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "little"))
        digest.update(part)
    return digest.hexdigest()


def _stat_identity_material(metadata: os.stat_result) -> tuple[str, bytes]:
    if os.name == "nt":
        scheme = "windows-file-id"
    elif os.name == "posix":
        scheme = "unix-dev-inode"
    else:
        scheme = "stat-dev-inode"
    material = int(metadata.st_dev).to_bytes(16, "little", signed=False)
    material += int(metadata.st_ino).to_bytes(16, "little", signed=False)
    return scheme, material


def target_identity_from_stat(metadata: os.stat_result) -> TargetIdentity:
    scheme, material = _stat_identity_material(metadata)
    return TargetIdentity(key=_digest([scheme.encode("ascii"), material]), scheme=scheme)


def target_identity(path: PathLike) -> TargetIdentity:
    return target_identity_from_stat(os.stat(native_path(path)))


def name_identity(path: PathLike) -> NameIdentity:
    native = native_path(path)
    parent = os.path.dirname(native) or (b"." if isinstance(native, bytes) else ".")
    canonical_parent = os.path.realpath(parent)
    parent_stat = os.stat(canonical_parent)
    parent_scheme, parent_material = _stat_identity_material(parent_stat)
    basename = os.path.basename(native)
    if os.name == "nt":
        normalized = os.fsdecode(basename).casefold().rstrip(" .").encode("utf-8")
        scheme = "windows-parent-name"
    else:
        normalized = os.fsencode(basename)
        scheme = "unix-parent-name" if os.name == "posix" else "parent-name"
    return NameIdentity(
        key=_digest([parent_scheme.encode("ascii"), parent_material, normalized]),
        scheme=scheme,
    )


def project_identity(root: PathLike) -> ProjectIdentity:
    native = os.path.realpath(native_path(root))
    metadata = os.stat(native)
    scheme, material = _stat_identity_material(metadata)
    return ProjectIdentity(
        key=_digest([b"project", scheme.encode("ascii"), material]),
        scheme=f"project-{scheme}",
    )


def present_version(
    requested_path: PathLike,
    target_metadata: os.stat_result,
    *,
    digest: str,
    metadata_digest: str,
) -> PresentVersion:
    return PresentVersion(
        name_identity=name_identity(requested_path),
        target_identity=target_identity_from_stat(target_metadata),
        size=target_metadata.st_size,
        mtime_ns=target_metadata.st_mtime_ns,
        digest=digest,
        metadata_digest=metadata_digest,
    )


def same_open_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        target_identity_from_stat(before) == target_identity_from_stat(after)
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and stat.S_IMODE(before.st_mode) == stat.S_IMODE(after.st_mode)
        and getattr(before, "st_uid", None) == getattr(after, "st_uid", None)
        and getattr(before, "st_gid", None) == getattr(after, "st_gid", None)
    )


def default_project_root(target: PathLike, configured_root: Optional[PathLike] = None) -> NativePath:
    if configured_root is not None:
        return native_path(configured_root)
    native = native_path(target)
    return os.path.dirname(native) or (b"." if isinstance(native, bytes) else ".")


__all__ = [
    "PathLike",
    "default_project_root",
    "name_identity",
    "native_path",
    "path_token",
    "present_version",
    "project_identity",
    "resolve_existing_target",
    "same_open_file",
    "target_identity",
    "target_identity_from_stat",
]
