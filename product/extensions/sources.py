"""Canonical provenance and approval gate for Product extension files."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from mote.contracts.agent import ApprovedSourceIdentity


class ExtensionKind(Enum):
    AGENT = "agent"
    SKILL = "skill"
    HOOK = "hook"
    MCP = "mcp"


class ExtensionScope(Enum):
    BUILTIN = "builtin"
    USER = "user"
    PROJECT = "project"


class ExtensionTrustDecision(Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ExtensionApproval:
    kind: ExtensionKind
    canonical_path: Path
    device: int
    inode: int
    content_digest: str
    principal: str

    def __post_init__(self) -> None:
        if not self.canonical_path.is_absolute():
            raise ValueError("extension approval path must be absolute")
        if type(self.device) is not int or self.device < 0 or type(self.inode) is not int or self.inode <= 0:
            raise ValueError("extension approval device/inode identity is invalid")
        if not self.content_digest.startswith("sha256:"):
            raise ValueError("extension approval digest must use sha256")
        if not self.principal.strip():
            raise ValueError("extension approval principal is required")


@dataclass(frozen=True, slots=True)
class ExtensionSource:
    kind: ExtensionKind
    scope: ExtensionScope
    canonical_path: Path
    device: int
    inode: int
    content_digest: str
    decision: ExtensionTrustDecision
    approval_principal: str | None
    content: bytes

    @property
    def approved(self) -> bool:
        return self.decision is ExtensionTrustDecision.APPROVED

    def approved_identity(self) -> ApprovedSourceIdentity:
        if not self.approved or self.approval_principal is None:
            raise ValueError("rejected extension source has no approved identity")
        return ApprovedSourceIdentity(
            canonical_name=str(self.canonical_path),
            device=self.device,
            inode=self.inode,
            content_digest=self.content_digest,
            approval_principal=self.approval_principal,
        )


@dataclass(frozen=True, slots=True)
class ApprovedExtensionSnapshot:
    approvals: tuple[ExtensionApproval, ...] = ()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class ExtensionSourcePolicy:
    """Resolve files once and admit only canonical, digest-bound sources."""

    def __init__(
        self,
        *,
        user_root: Path,
        builtin_roots: tuple[Path, ...],
        snapshot: ApprovedExtensionSnapshot = ApprovedExtensionSnapshot(),
    ) -> None:
        self._user_root = user_root.resolve()
        self._builtin_roots = tuple(root.resolve() for root in builtin_roots)
        self._approvals = {
            (
                approval.kind,
                approval.canonical_path,
                approval.device,
                approval.inode,
                approval.content_digest,
            ): approval.principal
            for approval in snapshot.approvals
        }

    def inspect(self, kind: ExtensionKind, path: Path) -> ExtensionSource:
        canonical = path.resolve(strict=True)
        descriptor = os.open(
            canonical,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("extension source must be a regular file")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            content = b"".join(chunks)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or after.st_size != len(content):
            raise RuntimeError(f"extension source changed while reading: {canonical}")
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        scope = self._scope(canonical)
        principal: str | None = None
        approved = False
        if scope is ExtensionScope.BUILTIN:
            approved = True
            principal = "mote:built-in"
        elif scope is ExtensionScope.USER:
            approved = opened.st_uid == os.getuid() and opened.st_mode & 0o022 == 0
            principal = "mote:user-install" if approved else None
        else:
            principal = self._approvals.get((kind, canonical, opened.st_dev, opened.st_ino, digest))
            approved = principal is not None
        return ExtensionSource(
            kind=kind,
            scope=scope,
            canonical_path=canonical,
            device=opened.st_dev,
            inode=opened.st_ino,
            content_digest=digest,
            decision=(ExtensionTrustDecision.APPROVED if approved else ExtensionTrustDecision.REJECTED),
            approval_principal=principal,
            content=content,
        )

    def admitted_files(self, kind: ExtensionKind, paths: tuple[Path, ...] | list[Path]) -> tuple[ExtensionSource, ...]:
        admitted: dict[tuple[int, int], ExtensionSource] = {}
        for path in paths:
            try:
                source = self.inspect(kind, path)
            except OSError:
                continue
            if source.approved:
                admitted[(source.device, source.inode)] = source
        return tuple(admitted.values())

    def _scope(self, canonical: Path) -> ExtensionScope:
        if any(_within(canonical, root) for root in self._builtin_roots):
            return ExtensionScope.BUILTIN
        if _within(canonical, self._user_root):
            return ExtensionScope.USER
        return ExtensionScope.PROJECT


__all__ = [
    "ApprovedExtensionSnapshot",
    "ExtensionApproval",
    "ExtensionKind",
    "ExtensionScope",
    "ExtensionSource",
    "ExtensionSourcePolicy",
    "ExtensionTrustDecision",
]
