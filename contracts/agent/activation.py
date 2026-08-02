"""Approved, provenance-bound declarations crossing Product into Runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

DeclarationT = TypeVar("DeclarationT")


@dataclass(frozen=True, slots=True)
class ApprovedSourceIdentity:
    """Immutable identity of bytes admitted by the Product trust policy."""

    canonical_name: str
    device: int
    inode: int
    content_digest: str
    approval_principal: str

    def __post_init__(self) -> None:
        if not self.canonical_name.startswith("/"):
            raise ValueError("approved source name must be canonical and absolute")
        if self.device < 0 or self.inode <= 0:
            raise ValueError("approved source filesystem identity is invalid")
        if not self.content_digest.startswith("sha256:"):
            raise ValueError("approved source digest must use sha256")
        if not self.approval_principal.strip():
            raise ValueError("approved source principal is required")


@dataclass(frozen=True, slots=True)
class ApprovedDeclaration(Generic[DeclarationT]):
    """A decoded declaration inseparably bound to all authoritative sources."""

    value: DeclarationT
    sources: tuple[ApprovedSourceIdentity, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources))
        if not self.sources:
            raise ValueError("approved declaration requires source provenance")


__all__ = ["ApprovedDeclaration", "ApprovedSourceIdentity"]
