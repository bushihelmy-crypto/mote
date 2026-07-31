"""File-domain value contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Optional, Tuple, Union

from mote.contracts.content import ContentIdentity

NativePath = Union[str, bytes]


class LockMode(StrEnum):
    SHARED = "shared"
    EXCLUSIVE = "exclusive"


class EncodingSource(StrEnum):
    BOM = "bom"
    EXPLICIT = "explicit"
    UTF8 = "utf8"
    DETECTED = "detected"
    FALLBACK = "fallback"


class FileChangeKind(StrEnum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


class FileChangeAttribution(StrEnum):
    EXTERNAL = "external"
    MANAGED = "managed"


@dataclass(frozen=True)
class PathToken:
    """A display spelling plus a native, losslessly round-trippable path."""

    display: str
    native: NativePath


@dataclass(frozen=True, order=True)
class NameIdentity:
    """Stable identity of one directory entry, including an absent entry."""

    key: str
    scheme: str


@dataclass(frozen=True, order=True)
class TargetIdentity:
    """Stable filesystem identity of an existing target."""

    key: str
    scheme: str


@dataclass(frozen=True, order=True)
class ProjectIdentity:
    """Stable identity of the project barrier that contains a target."""

    key: str
    scheme: str


@dataclass(frozen=True)
class AbsentVersion:
    name_identity: NameIdentity


@dataclass(frozen=True)
class PresentVersion:
    name_identity: NameIdentity
    target_identity: TargetIdentity
    size: int
    mtime_ns: int
    digest: str
    metadata_digest: str


FileVersion = Union[AbsentVersion, PresentVersion]


@dataclass(frozen=True)
class FileVersionTransition:
    path: str
    prior: FileVersion
    current: FileVersion


@dataclass(frozen=True)
class EncodingDecision:
    label: str
    bom: bytes
    source: EncodingSource
    confidence: Optional[float] = None


@dataclass(frozen=True)
class NewlineProfile:
    lf: int
    crlf: int
    cr: int

    @property
    def dominant(self) -> str:
        if self.crlf > self.lf and self.crlf >= self.cr:
            return "\r\n"
        if self.cr > self.lf:
            return "\r"
        return "\n"


@dataclass(frozen=True)
class EditableTextSnapshot:
    text: str
    logical_to_raw_boundaries: Tuple[int, ...]
    encoding: EncodingDecision
    newline_profile: NewlineProfile


@dataclass(frozen=True)
class FileSnapshot:
    requested_path: PathToken
    target_path: PathToken
    project_identity: ProjectIdentity
    version: PresentVersion
    artifact: ContentIdentity
    metadata: ContentIdentity
    encoding: Optional[EncodingDecision] = None

    def __post_init__(self) -> None:
        if self.artifact.digest != self.version.digest:
            raise ValueError("snapshot content artifact digest does not match version")
        if self.artifact.size != self.version.size:
            raise ValueError("snapshot content artifact size does not match version")
        if self.metadata.digest != self.version.metadata_digest:
            raise ValueError("snapshot metadata artifact digest does not match version")


@dataclass(frozen=True, order=True)
class LockSpec:
    """One lock request. Lower ``level`` values are always acquired first."""

    level: int
    key: str
    mode: LockMode
    label: str = ""
