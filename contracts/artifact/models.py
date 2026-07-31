"""Stable models for immutable, revisioned artifacts."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PureWindowsPath

from mote.contracts.content import ContentIdentity

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPRESENTATION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _validate_digest(digest: str) -> None:
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("artifact digest must be a lowercase SHA-256 hex value")


def _validate_suggested_name(name: str) -> None:
    if not name:
        return
    if len(name) > 255 or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("artifact suggested_name must be a plain filename")


def _validate_content_ref(content_ref: str) -> None:
    if (
        not isinstance(content_ref, str)
        or not content_ref
        or len(content_ref) > 2048
        or any(character.isspace() or ord(character) < 32 for character in content_ref)
    ):
        raise ValueError("artifact content_ref must be a non-empty opaque reference")
    scheme, separator, _ = content_ref.partition(":")
    has_scheme = bool(separator and len(scheme) > 1 and re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]{0,31}", scheme))
    if (
        Path(content_ref).is_absolute()
        or (PureWindowsPath(content_ref).is_absolute() and not has_scheme)
        or scheme.lower() == "file"
        or (("/" in content_ref or "\\" in content_ref) and not has_scheme)
    ):
        raise ValueError("artifact content_ref must not expose a filesystem path")


def _validate_mime_type(mime_type: str) -> None:
    if (
        type(mime_type) is not str
        or not mime_type
        or "/" not in mime_type
        or any(character.isspace() or ord(character) < 32 for character in mime_type)
    ):
        raise ValueError("artifact mime_type is invalid")


class ArtifactRetention(StrEnum):
    EPHEMERAL = "ephemeral"
    SESSION = "session"
    PROJECT = "project"
    PINNED = "pinned"


class ArtifactSensitivity(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    SECRET = "secret"


class ArtifactPublicationState(StrEnum):
    QUEUED = "queued"
    FAILED = "failed"
    COMPLETED = "completed"
    DEAD_LETTER = "dead_letter"


class ContentLocator(str):
    def __new__(cls, value: str):
        _validate_content_ref(value)
        return str.__new__(cls, value)


@dataclass(frozen=True, slots=True)
class ArtifactContentRef:
    identity: ContentIdentity
    locator: ContentLocator

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ContentIdentity):
            raise TypeError("artifact content ref requires a ContentIdentity")
        object.__setattr__(self, "locator", ContentLocator(self.locator))

    @property
    def content_ref(self) -> str:
        return self.locator


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    revision: int
    representation: str
    kind: str
    mime_type: str
    content_ref: str
    digest: str
    size: int
    retention: ArtifactRetention = ArtifactRetention.SESSION
    sensitivity: ArtifactSensitivity = ArtifactSensitivity.PRIVATE
    suggested_name: str = ""

    def __post_init__(self) -> None:
        if not all(
            (
                self.artifact_id,
                self.representation,
                self.kind,
                self.mime_type,
                self.content_ref,
                self.digest,
            )
        ):
            raise ValueError("artifact identity, representation and content fields must be non-empty")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("artifact revision must be a positive integer")
        if type(self.size) is not int or self.size < 0:
            raise ValueError("artifact size must be a non-negative integer")
        if _IDENTIFIER.fullmatch(self.artifact_id) is None:
            raise ValueError("artifact_id is invalid")
        if _REPRESENTATION.fullmatch(self.representation) is None:
            raise ValueError("artifact representation is invalid")
        if _IDENTIFIER.fullmatch(self.kind) is None:
            raise ValueError("artifact kind is invalid")
        _validate_mime_type(self.mime_type)
        _validate_content_ref(self.content_ref)
        _validate_digest(self.digest)
        _validate_suggested_name(self.suggested_name)
        object.__setattr__(self, "retention", ArtifactRetention(self.retention))
        object.__setattr__(self, "sensitivity", ArtifactSensitivity(self.sensitivity))

    @property
    def readable(self) -> str:
        return f"artifact:{self.artifact_id}@{self.revision}:{self.representation}"


@dataclass(frozen=True, slots=True)
class ArtifactResolutionPolicy:
    """Explicit trust and resource bounds for resolving Artifact bytes."""

    max_bytes: int
    allowed_sensitivities: frozenset[ArtifactSensitivity]

    def __post_init__(self) -> None:
        if type(self.max_bytes) is not int or self.max_bytes < 0:
            raise ValueError("artifact resolution max_bytes must be non-negative")
        sensitivities = frozenset(ArtifactSensitivity(item) for item in self.allowed_sensitivities)
        if not sensitivities:
            raise ValueError("artifact resolution requires at least one allowed sensitivity")
        object.__setattr__(self, "allowed_sensitivities", sensitivities)


@dataclass(frozen=True, slots=True)
class ResolvedArtifact:
    """Verified bytes resolved from one opaque, durable Artifact reference."""

    ref: ArtifactRef
    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.ref, ArtifactRef):
            raise TypeError("resolved artifact requires an ArtifactRef")
        if type(self.content) is not bytes:
            raise TypeError("resolved artifact content must be bytes")
        if len(self.content) != self.ref.size:
            raise ValueError("resolved artifact size does not match its reference")
        if hashlib.sha256(self.content).hexdigest() != self.ref.digest:
            raise ValueError("resolved artifact digest does not match its reference")


@dataclass(frozen=True, slots=True)
class ArtifactRepresentationInput:
    representation: str
    kind: str
    mime_type: str
    content: bytes = field(repr=False)
    suggested_name: str = ""

    def __post_init__(self) -> None:
        if not self.representation or not self.kind or not self.mime_type:
            raise ValueError("artifact representation, kind and mime_type must be non-empty")
        if type(self.content) is not bytes:
            raise TypeError("artifact representation content must be bytes")
        if _REPRESENTATION.fullmatch(self.representation) is None:
            raise ValueError("artifact representation is invalid")
        if _IDENTIFIER.fullmatch(self.kind) is None:
            raise ValueError("artifact kind is invalid")
        _validate_mime_type(self.mime_type)
        _validate_suggested_name(self.suggested_name)


@dataclass(frozen=True, slots=True)
class ArtifactRepresentationIntent:
    """One already-materialized representation held by the trusted Artifact CAS."""

    representation: str
    kind: str
    mime_type: str
    content: ArtifactContentRef
    suggested_name: str = ""

    def __post_init__(self) -> None:
        if not self.representation or not self.kind or not self.mime_type:
            raise ValueError("artifact representation, kind and mime_type must be non-empty")
        if not isinstance(self.content, ArtifactContentRef):
            raise TypeError("artifact representation intent requires a content ref")
        if _REPRESENTATION.fullmatch(self.representation) is None:
            raise ValueError("artifact representation is invalid")
        if _IDENTIFIER.fullmatch(self.kind) is None:
            raise ValueError("artifact kind is invalid")
        _validate_mime_type(self.mime_type)
        _validate_suggested_name(self.suggested_name)


@dataclass(frozen=True, slots=True)
class ArtifactPublicationIntent:
    """A durable publication request whose bytes already live in the trusted CAS.

    This is the materialized output of a Runtime projection handler. It is not
    the versioned projection instruction stored with a Runtime commit fact.
    """

    publication_id: str
    representations: tuple[ArtifactRepresentationIntent, ...]
    artifact_id: str = ""
    expected_revision: int | None = None
    retention: ArtifactRetention = ArtifactRetention.SESSION
    sensitivity: ArtifactSensitivity = ArtifactSensitivity.PRIVATE
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "representations", tuple(self.representations))
        object.__setattr__(self, "retention", ArtifactRetention(self.retention))
        object.__setattr__(self, "sensitivity", ArtifactSensitivity(self.sensitivity))
        if (
            type(self.publication_id) is not str
            or not self.publication_id
            or len(self.publication_id) > 256
            or any(ord(character) < 32 for character in self.publication_id)
        ):
            raise ValueError("artifact publication_id is invalid")
        if not self.representations:
            raise ValueError("artifact publication requires at least one representation")
        names = [item.representation for item in self.representations]
        if len(names) != len(set(names)):
            raise ValueError("artifact representation names must be unique")
        if self.expected_revision is not None and (
            type(self.expected_revision) is not int or self.expected_revision < 0
        ):
            raise ValueError("expected artifact revision must be a non-negative integer")
        if self.expected_revision is not None and not self.artifact_id:
            raise ValueError("expected_revision requires artifact_id")
        if self.artifact_id and _IDENTIFIER.fullmatch(self.artifact_id) is None:
            raise ValueError("artifact_id is invalid")
        if len(self.idempotency_key) > 256 or "\x00" in self.idempotency_key:
            raise ValueError("artifact idempotency_key is invalid")


@dataclass(frozen=True, slots=True)
class ArtifactPublishRequest:
    representations: tuple[ArtifactRepresentationInput, ...]
    artifact_id: str = ""
    expected_revision: int | None = None
    retention: ArtifactRetention = ArtifactRetention.SESSION
    sensitivity: ArtifactSensitivity = ArtifactSensitivity.PRIVATE
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "representations", tuple(self.representations))
        object.__setattr__(self, "retention", ArtifactRetention(self.retention))
        object.__setattr__(self, "sensitivity", ArtifactSensitivity(self.sensitivity))
        if not self.representations:
            raise ValueError("artifact publication requires at least one representation")
        names = [item.representation for item in self.representations]
        if len(names) != len(set(names)):
            raise ValueError("artifact representation names must be unique")
        if self.expected_revision is not None and (
            type(self.expected_revision) is not int or self.expected_revision < 0
        ):
            raise ValueError("expected artifact revision must be a non-negative integer")
        if self.expected_revision is not None and not self.artifact_id:
            raise ValueError("expected_revision requires artifact_id")
        if self.artifact_id and _IDENTIFIER.fullmatch(self.artifact_id) is None:
            raise ValueError("artifact_id is invalid")
        if len(self.idempotency_key) > 256 or "\x00" in self.idempotency_key:
            raise ValueError("artifact idempotency_key is invalid")


@dataclass(frozen=True, slots=True)
class ArtifactRevision:
    artifact_id: str
    revision: int
    representations: tuple[ArtifactRef, ...]

    def __post_init__(self) -> None:
        if not self.artifact_id or type(self.revision) is not int or self.revision < 1:
            raise ValueError("artifact revision identity is invalid")
        if not self.representations:
            raise ValueError("artifact revision requires at least one representation")
        if any(item.artifact_id != self.artifact_id or item.revision != self.revision for item in self.representations):
            raise ValueError("artifact representations must share one artifact revision")

    def get(self, representation: str) -> ArtifactRef:
        for item in self.representations:
            if item.representation == representation:
                return item
        raise KeyError(representation)


@dataclass(frozen=True, slots=True)
class ArtifactPublication:
    publication_id: str
    request: ArtifactPublishRequest
    state: ArtifactPublicationState = ArtifactPublicationState.QUEUED
    attempts: int = 0
    last_error: str = ""
    result_artifact_id: str = ""
    result_revision: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.publication_id) is not str
            or not self.publication_id
            or len(self.publication_id) > 256
            or any(ord(character) < 32 for character in self.publication_id)
        ):
            raise ValueError("artifact publication_id is invalid")
        object.__setattr__(self, "state", ArtifactPublicationState(self.state))
        if type(self.attempts) is not int or self.attempts < 0:
            raise ValueError("artifact publication attempts must be non-negative")
        if type(self.last_error) is not str or len(self.last_error) > 4096:
            raise ValueError("artifact publication last_error is invalid")
        if self.result_artifact_id and _IDENTIFIER.fullmatch(self.result_artifact_id) is None:
            raise ValueError("artifact publication result artifact_id is invalid")
        if self.result_revision is not None and (type(self.result_revision) is not int or self.result_revision < 1):
            raise ValueError("artifact publication result revision is invalid")
        if bool(self.result_artifact_id) != (self.result_revision is not None):
            raise ValueError("artifact publication result identity is incomplete")
        if self.state is ArtifactPublicationState.COMPLETED and not self.result_artifact_id:
            raise ValueError("completed artifact publication requires a result")


@dataclass(frozen=True, slots=True)
class ArtifactPublicationResult:
    publication_id: str
    revision: ArtifactRevision


@dataclass(frozen=True, slots=True)
class ArtifactPublicationFailure:
    publication_id: str
    error: str
    retryable: bool = False
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class ArtifactPublicationReconcileResult:
    published: tuple[ArtifactPublicationResult, ...] = ()
    failed: tuple[ArtifactPublicationFailure, ...] = ()
    dead_lettered: tuple[ArtifactPublicationFailure, ...] = ()


__all__ = [
    "ArtifactContentRef",
    "ContentLocator",
    "ArtifactPublication",
    "ArtifactPublicationFailure",
    "ArtifactPublicationIntent",
    "ArtifactPublicationReconcileResult",
    "ArtifactPublicationResult",
    "ArtifactPublicationState",
    "ArtifactPublishRequest",
    "ArtifactRef",
    "ArtifactResolutionPolicy",
    "ArtifactRepresentationInput",
    "ArtifactRepresentationIntent",
    "ArtifactRetention",
    "ArtifactRevision",
    "ArtifactSensitivity",
    "ResolvedArtifact",
]
