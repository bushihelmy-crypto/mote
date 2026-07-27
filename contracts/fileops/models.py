"""Cross-boundary data contracts for the File Operations bounded context."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Optional, Tuple, Union

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


class MutationKind(StrEnum):
    CREATE = "create"
    REPLACE = "replace"
    DELETE = "delete"


class RecoveryPolicy(StrEnum):
    ROLLBACK_INCOMPLETE = "rollback_incomplete"


class FileOperationKind(StrEnum):
    MUTATION = "mutation"
    REWIND = "rewind"


class FileChangeKind(StrEnum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


class FileChangeAttribution(StrEnum):
    EXTERNAL = "external"
    MANAGED = "managed"


class ArtifactGarbageCollectionState(StrEnum):
    NEVER_RUN = "never_run"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SearchOutputMode(StrEnum):
    FILES_WITH_MATCHES = "files_with_matches"
    CONTENT = "content"
    COUNT = "count"
    ONLY_MATCHING = "only_matching"


class SearchSkipReason(StrEnum):
    BINARY = "binary"
    CHANGED = "changed"
    ENCODING = "encoding"
    EXTRACTOR_UNAVAILABLE = "extractor_unavailable"
    EXTRACTION = "extraction"
    RESOURCE_LIMIT = "resource_limit"
    IO = "io"


class SearchStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class ByteViewMode(StrEnum):
    RAW = "raw"
    HEX = "hex"


class ReadViewStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class PdfViewMode(StrEnum):
    TEXT = "text"
    RENDER = "render"


class TextViewMode(StrEnum):
    TEXT = "text"
    DOCUMENT = "document"


@dataclass(frozen=True)
class ExtractionBudget:
    max_archive_uncompressed_bytes: int
    max_output_bytes: int

    def __post_init__(self) -> None:
        for name, value in (
            ("max_archive_uncompressed_bytes", self.max_archive_uncompressed_bytes),
            ("max_output_bytes", self.max_output_bytes),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


class ReadCursorKind(StrEnum):
    TEXT = "text"
    RAW = "raw"
    HEX = "hex"
    PDF_TEXT = "pdf_text"
    PDF_RENDER = "pdf_render"


@dataclass(frozen=True)
class TextReadRequest:
    offset: Optional[int] = None
    limit: Optional[int] = None
    encoding: Optional[str] = None
    fallback_encoding: Optional[str] = None


@dataclass(frozen=True)
class ByteReadRequest:
    mode: ByteViewMode
    offset: Optional[int] = None
    limit: Optional[int] = None


@dataclass(frozen=True)
class PdfReadRequest:
    mode: PdfViewMode
    pages: str = ""
    dpi: int = 144
    limit: Optional[int] = None

    def __post_init__(self) -> None:
        if self.pages.strip() and self.limit is not None:
            raise ValueError("PDF pages cannot be combined with a page limit")


@dataclass(frozen=True)
class ContinueReadRequest:
    cursor: str
    limit: Optional[int] = None


ReadRequest = Union[
    TextReadRequest,
    ByteReadRequest,
    PdfReadRequest,
    ContinueReadRequest,
]


class TransactionStatus(StrEnum):
    PREPARED = "prepared"
    COMMITTED = "committed"
    ABORTED = "aborted"
    IN_DOUBT = "in_doubt"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTING = "rejecting"
    REJECTED = "rejected"


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
class BlobRef:
    digest: str
    size: int


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
    artifact: BlobRef
    metadata: BlobRef
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


@dataclass(frozen=True)
class CreateMutation:
    requested_path: PathToken
    target_path: PathToken
    project_identity: ProjectIdentity
    expected_version: AbsentVersion
    after: BlobRef
    metadata: BlobRef

    kind: ClassVar[MutationKind] = MutationKind.CREATE


@dataclass(frozen=True)
class ReplaceMutation:
    before: FileSnapshot
    after: BlobRef

    kind: ClassVar[MutationKind] = MutationKind.REPLACE

    @property
    def requested_path(self) -> PathToken:
        return self.before.requested_path

    @property
    def target_path(self) -> PathToken:
        return self.before.target_path

    @property
    def project_identity(self) -> ProjectIdentity:
        return self.before.project_identity

    @property
    def expected_version(self) -> PresentVersion:
        return self.before.version


@dataclass(frozen=True)
class DeleteMutation:
    before: FileSnapshot

    kind: ClassVar[MutationKind] = MutationKind.DELETE

    @property
    def requested_path(self) -> PathToken:
        return self.before.requested_path

    @property
    def target_path(self) -> PathToken:
        return self.before.target_path

    @property
    def project_identity(self) -> ProjectIdentity:
        return self.before.project_identity

    @property
    def expected_version(self) -> PresentVersion:
        return self.before.version


Mutation = Union[
    CreateMutation,
    ReplaceMutation,
    DeleteMutation,
]


@dataclass(frozen=True)
class MutationSet:
    transaction_id: str
    session_id: str
    source: str
    mutations: Tuple[Mutation, ...]
    recovery_policy: RecoveryPolicy = RecoveryPolicy.ROLLBACK_INCOMPLETE

    def __post_init__(self) -> None:
        for field, value in (
            ("transaction_id", self.transaction_id),
            ("session_id", self.session_id),
            ("source", self.source),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"mutation set {field} must be a non-empty string")
        if not isinstance(self.recovery_policy, RecoveryPolicy):
            raise TypeError("mutation set recovery_policy is invalid")
        if type(self.mutations) is not tuple or not self.mutations:
            raise ValueError("mutation set must contain at least one mutation")
        for mutation in self.mutations:
            _validate_mutation_scope(mutation)
        canonical = tuple(sorted(self.mutations, key=_mutation_sort_key))
        if canonical != self.mutations:
            object.__setattr__(self, "mutations", canonical)
        names: set[NameIdentity] = set()
        targets: set[TargetIdentity] = set()
        for mutation in canonical:
            name = mutation.expected_version.name_identity
            if name in names:
                raise ValueError("mutation set contains a duplicate name identity")
            names.add(name)
            expected = mutation.expected_version
            if isinstance(expected, PresentVersion):
                if expected.target_identity in targets:
                    raise ValueError("mutation set contains a duplicate target identity")
                targets.add(expected.target_identity)


@dataclass(frozen=True)
class TransactionRecord:
    mutation_set: MutationSet
    status: TransactionStatus
    hunks: Tuple[HunkRecord, ...] = ()
    committed_versions: Tuple[FileVersion, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.mutation_set, MutationSet):
            raise TypeError("transaction record mutation_set is invalid")
        if not isinstance(self.status, TransactionStatus):
            raise TypeError("transaction record status is invalid")
        if self.status == TransactionStatus.COMMITTED:
            validate_committed_versions(self.mutation_set, self.committed_versions)
        elif self.committed_versions:
            raise ValueError("only a committed transaction may contain committed versions")


@dataclass(frozen=True)
class MutationResult:
    transaction_id: str
    status: TransactionStatus
    versions: Tuple[FileVersion, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class EditCommitChange:
    """One committed edit rendered from the plan's sealed B0/B1 artifacts."""

    path: PathToken
    old: str
    new: str
    post_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, PathToken):
            raise TypeError("edit commit change path is invalid")
        if type(self.old) is not str or type(self.new) is not str:
            raise TypeError("edit commit change content must be text")
        if (
            type(self.post_digest) is not str
            or len(self.post_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.post_digest)
        ):
            raise ValueError("edit commit change digest is invalid")


@dataclass(frozen=True)
class EditCommitOutcome:
    """Commit result plus presentation facts derived inside File Operations."""

    result: MutationResult
    changes: Tuple[EditCommitChange, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.result, MutationResult):
            raise TypeError("edit commit outcome result is invalid")
        if type(self.changes) is not tuple or any(not isinstance(change, EditCommitChange) for change in self.changes):
            raise TypeError("edit commit outcome changes are invalid")
        if self.result.status != TransactionStatus.COMMITTED and self.changes:
            raise ValueError("only a committed edit may expose content changes")
        if self.changes and len(self.changes) != len(self.result.versions):
            raise ValueError("edit commit changes do not match committed versions")


def _identity_text(identity: NameIdentity | TargetIdentity | ProjectIdentity) -> None:
    if type(identity.key) is not str or not identity.key:
        raise ValueError("file identity key must be a non-empty string")
    if type(identity.scheme) is not str or not identity.scheme:
        raise ValueError("file identity scheme must be a non-empty string")


def _validate_blob(ref: BlobRef) -> None:
    if not isinstance(ref, BlobRef):
        raise TypeError("mutation artifact reference is invalid")
    if (
        type(ref.digest) is not str
        or len(ref.digest) != 64
        or any(character not in "0123456789abcdef" for character in ref.digest)
    ):
        raise ValueError("mutation artifact digest is invalid")
    if type(ref.size) is not int or not 0 <= ref.size < (1 << 63):
        raise ValueError("mutation artifact size is invalid")


def _validate_mutation_scope(mutation: Mutation) -> None:
    if not isinstance(mutation, (CreateMutation, ReplaceMutation, DeleteMutation)):
        raise TypeError("mutation set contains an unsupported mutation")
    for path in (mutation.requested_path, mutation.target_path):
        if type(path.display) is not str or not path.display:
            raise ValueError("mutation path display must be a non-empty string")
        if type(path.native) not in (str, bytes) or not path.native:
            raise ValueError("mutation native path must be non-empty text or bytes")
    _identity_text(mutation.project_identity)
    _identity_text(mutation.expected_version.name_identity)
    if isinstance(mutation.expected_version, PresentVersion):
        _identity_text(mutation.expected_version.target_identity)
    if isinstance(mutation, CreateMutation):
        if mutation.requested_path != mutation.target_path:
            raise ValueError("create mutation requested and target paths must match")
        _validate_blob(mutation.after)
        _validate_blob(mutation.metadata)
        return
    _validate_blob(mutation.before.artifact)
    _validate_blob(mutation.before.metadata)
    if isinstance(mutation, ReplaceMutation):
        _validate_blob(mutation.after)


def _mutation_sort_key(mutation: Mutation) -> tuple[str, ...]:
    expected = mutation.expected_version
    target = (
        ("", "")
        if isinstance(expected, AbsentVersion)
        else (expected.target_identity.scheme, expected.target_identity.key)
    )
    return (
        mutation.project_identity.scheme,
        mutation.project_identity.key,
        mutation.requested_path.display,
        expected.name_identity.scheme,
        expected.name_identity.key,
        target[0],
        target[1],
        mutation.kind.value,
    )


def validate_committed_versions(
    mutation_set: MutationSet,
    versions: Tuple[FileVersion, ...],
) -> None:
    if type(versions) is not tuple or len(versions) != len(mutation_set.mutations):
        raise ValueError("committed versions must correspond one-to-one with mutations")
    for mutation, version in zip(mutation_set.mutations, versions):
        expected_name = mutation.expected_version.name_identity
        if version.name_identity != expected_name:
            raise ValueError("committed version name identity does not match mutation")
        if isinstance(mutation, DeleteMutation):
            if not isinstance(version, AbsentVersion):
                raise ValueError("delete mutation must commit an absent version")
        elif not isinstance(version, PresentVersion):
            raise ValueError("create and replace mutations must commit present versions")


@dataclass(frozen=True)
class SearchRow:
    path: PathToken
    version: Optional[PresentVersion]
    line_number: Optional[int]
    text: str
    matched_text: str
    occurrence_count: int
    is_context: bool = False


@dataclass(frozen=True)
class SearchSkippedFile:
    path: PathToken
    reason: SearchSkipReason
    detail: str


@dataclass(frozen=True)
class SearchSummary:
    discovered_files: int
    scanned_files: int
    matched_files: int
    total_occurrences: int
    skipped_files: int
    complete: bool = True
    termination: str = ""


@dataclass(frozen=True)
class SearchResult:
    rows: Tuple[SearchRow, ...]
    files: Tuple[PathToken, ...]
    summary: SearchSummary
    skipped: Tuple[SearchSkippedFile, ...]
    artifact: BlobRef
    skipped_artifact: BlobRef
    skipped_truncated: bool
    output_mode: SearchOutputMode
    content_search: bool
    status: SearchStatus
    next_cursor: Optional[str] = None


@dataclass(frozen=True)
class FileByteView:
    snapshot: FileSnapshot
    mode: ByteViewMode
    status: ReadViewStatus
    offset: int
    next_offset: Optional[int]
    total_bytes: int
    data: bytes
    text: str = ""
    next_cursor: Optional[str] = None


@dataclass(frozen=True)
class PdfPageView:
    page_number: int
    text: str = ""
    lines: Tuple[str, ...] = ()
    png: bytes = b""
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class PdfView:
    snapshot: FileSnapshot
    mode: PdfViewMode
    status: ReadViewStatus
    total_pages: int
    pages: Tuple[PdfPageView, ...]
    next_pages: Optional[str] = None
    next_cursor: Optional[str] = None


@dataclass(frozen=True)
class FileTextView:
    snapshot: FileSnapshot
    mode: TextViewMode
    status: ReadViewStatus
    offset: int
    next_offset: Optional[int]
    total_lines: int
    lines: Tuple[str, ...]
    next_cursor: Optional[str] = None


@dataclass(frozen=True)
class RewindRecord:
    transaction_id: str
    session_id: str
    status: TransactionStatus
    project_identity: ProjectIdentity
    working_dir: str
    safety_commit: str
    target_commit: str
    prompt_index: int
    source_epoch: int
    external_paths: Tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class RewindResult:
    transaction_id: str
    status: TransactionStatus
    safety_commit: str
    target_commit: str
    external_paths: Tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class FileOperationsHealth:
    """Read-only readiness projection for one session's file-operation plane."""

    lock_backend: str
    journal_readable: bool
    journal_writable: bool
    artifact_readable: bool
    artifact_writable: bool
    artifact_catalog_readable: bool
    recovery_backlog: int
    in_doubt_transactions: Tuple[str, ...] = ()
    affected_paths: Tuple[str, ...] = ()
    cursor_registry_readable: bool = True
    timeline_epoch: int = 0
    active_cursor_leases: int = 0
    expired_cursor_leases: int = 0
    pinned_artifacts: int = 0
    pinned_bytes: int = 0
    nearest_cursor_expiry_ns: Optional[int] = None
    observed_snapshots: int = 0
    artifact_hard_limit_bytes: int = 0
    artifact_physical_bytes: int = 0
    artifact_reserved_bytes: int = 0
    artifact_staged_bytes: int = 0
    artifact_active_reservations: int = 0
    artifact_open_stages: int = 0
    artifact_catalog_generation: int = 0
    artifact_staging_objects: int = 0
    artifact_quarantined_objects: int = 0
    artifact_deleting_objects: int = 0
    artifact_quota_pressure: float = 0.0
    artifact_gc_state: ArtifactGarbageCollectionState = ArtifactGarbageCollectionState.NEVER_RUN
    artifact_gc_completed_at_ns: Optional[int] = None
    artifact_gc_quarantined_objects: int = 0
    artifact_gc_restored_objects: int = 0
    artifact_gc_deletion_candidates: int = 0
    artifact_gc_reclaimed_objects: int = 0
    artifact_gc_reclaimed_bytes: int = 0
    artifact_gc_failure: str = ""

    @property
    def ready(self) -> bool:
        return (
            self.journal_readable
            and self.journal_writable
            and self.artifact_readable
            and self.artifact_writable
            and self.artifact_catalog_readable
            and self.cursor_registry_readable
            and self.artifact_gc_state != ArtifactGarbageCollectionState.FAILED
            and self.recovery_backlog == 0
            and not self.in_doubt_transactions
        )


@dataclass(frozen=True)
class HunkRecord:
    """Versioned durable review projection for one attributed file hunk."""

    hunk_id: str
    path: str
    session_id: str
    tool_call_id: str
    turn_index: int
    source: str
    old_range: Tuple[int, int]
    new_range: Tuple[int, int]
    pre_hash: str
    post_hash: str
    expected_digest: str
    status: ReviewStatus = ReviewStatus.PENDING
    version: int = 1
    child_transaction_id: str = ""

    @property
    def is_agent(self) -> bool:
        return self.source == "agent"

    @property
    def is_external(self) -> bool:
        return self.source == "external"


__all__ = [
    "AbsentVersion",
    "ArtifactGarbageCollectionState",
    "BlobRef",
    "ByteReadRequest",
    "ByteViewMode",
    "EditableTextSnapshot",
    "EncodingDecision",
    "EncodingSource",
    "ExtractionBudget",
    "FileOperationKind",
    "FileByteView",
    "FileSnapshot",
    "FileTextView",
    "FileOperationsHealth",
    "FileVersion",
    "ContinueReadRequest",
    "CreateMutation",
    "DeleteMutation",
    "EditCommitChange",
    "EditCommitOutcome",
    "HunkRecord",
    "LockMode",
    "LockSpec",
    "MutationKind",
    "Mutation",
    "MutationResult",
    "NameIdentity",
    "NativePath",
    "NewlineProfile",
    "PathToken",
    "PdfPageView",
    "PdfReadRequest",
    "PdfView",
    "PdfViewMode",
    "PresentVersion",
    "ProjectIdentity",
    "RewindRecord",
    "RewindResult",
    "ReviewStatus",
    "ReadViewStatus",
    "ReadCursorKind",
    "ReadRequest",
    "ReplaceMutation",
    "SearchOutputMode",
    "SearchResult",
    "SearchRow",
    "SearchSkipReason",
    "SearchSkippedFile",
    "SearchStatus",
    "SearchSummary",
    "TargetIdentity",
    "TextViewMode",
    "TextReadRequest",
    "TransactionRecord",
    "TransactionStatus",
]
