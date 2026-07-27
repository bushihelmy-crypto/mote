"""Durable session events for managed file transactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from mote.contracts.fileops.models import BlobRef, FileVersion, HunkRecord, MutationSet, ProjectIdentity, ReviewStatus
from mote.contracts.fileops.serialization import (
    blob_from_dict,
    blob_to_dict,
    mutation_set_from_dict,
    mutation_set_to_dict,
    version_from_dict,
    version_to_dict,
)

FILE_TRANSACTION_PREPARED = "file_transaction_prepared"
FILE_TRANSACTION_COMMITTED = "file_transaction_committed"
FILE_TRANSACTION_ABORTED = "file_transaction_aborted"
FILE_TRANSACTION_IN_DOUBT = "file_transaction_in_doubt"
FILE_HISTORY_IMPORTED = "file_history_imported"
FILE_EDIT_PLAN_STORED = "file_edit_plan_stored"
HUNK_DETECTED = "hunk_detected"
HUNK_REVIEW_TRANSITIONED = "hunk_review_transitioned"
REWIND_PREPARED = "rewind_prepared"
REWIND_COMMITTED = "rewind_committed"
REWIND_ABORTED = "rewind_aborted"
REWIND_IN_DOUBT = "rewind_in_doubt"


def _require_keys(payload: dict[str, Any], keys: set[str]) -> None:
    if type(payload) is not dict or set(payload) != keys:
        raise ValueError("file operations event fields are not canonical")


def _text(payload: dict[str, Any], key: str, *, nonempty: bool = False) -> str:
    value = payload[key]
    if type(value) is not str or (nonempty and not value):
        raise ValueError(f"file operations event {key} is invalid")
    return value


def _integer(payload: dict[str, Any], key: str, *, minimum: int = 0) -> int:
    value = payload[key]
    if type(value) is not int or value < minimum:
        raise ValueError(f"file operations event {key} is invalid")
    return value


def _string_list(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    value = payload[key]
    if type(value) is not list or any(type(item) is not str for item in value):
        raise ValueError(f"file operations event {key} is invalid")
    return tuple(value)


def _range(payload: dict[str, Any], key: str) -> tuple[int, int]:
    value = payload[key]
    if type(value) is not list or len(value) != 2 or any(type(item) is not int or item < 0 for item in value):
        raise ValueError(f"file operations event {key} is invalid")
    return value[0], value[1]


def _hunk_payload(record: HunkRecord) -> dict[str, Any]:
    return {
        "hunk_id": record.hunk_id,
        "path": record.path,
        "session_id": record.session_id,
        "tool_call_id": record.tool_call_id,
        "turn_index": record.turn_index,
        "source": record.source,
        "old_range": list(record.old_range),
        "new_range": list(record.new_range),
        "pre_hash": record.pre_hash,
        "post_hash": record.post_hash,
        "expected_digest": record.expected_digest,
        "status": record.status.value,
        "version": record.version,
        "child_transaction_id": record.child_transaction_id,
    }


def _hunk_from_payload(payload: dict[str, Any]) -> HunkRecord:
    _require_keys(
        payload,
        {
            "hunk_id",
            "path",
            "session_id",
            "tool_call_id",
            "turn_index",
            "source",
            "old_range",
            "new_range",
            "pre_hash",
            "post_hash",
            "expected_digest",
            "status",
            "version",
            "child_transaction_id",
        },
    )
    return HunkRecord(
        hunk_id=_text(payload, "hunk_id", nonempty=True),
        path=_text(payload, "path", nonempty=True),
        session_id=_text(payload, "session_id", nonempty=True),
        tool_call_id=_text(payload, "tool_call_id"),
        turn_index=_integer(payload, "turn_index"),
        source=_text(payload, "source", nonempty=True),
        old_range=_range(payload, "old_range"),
        new_range=_range(payload, "new_range"),
        pre_hash=_text(payload, "pre_hash", nonempty=True),
        post_hash=_text(payload, "post_hash", nonempty=True),
        expected_digest=_text(payload, "expected_digest", nonempty=True),
        status=ReviewStatus(_text(payload, "status", nonempty=True)),
        version=_integer(payload, "version", minimum=1),
        child_transaction_id=_text(payload, "child_transaction_id"),
    )


@dataclass(frozen=True)
class FileTransactionPreparedEvent:
    mutation_set: MutationSet
    hunks: tuple[HunkRecord, ...] = ()

    type: ClassVar[str] = FILE_TRANSACTION_PREPARED

    def payload(self) -> dict[str, Any]:
        return {
            "mutation_set": mutation_set_to_dict(self.mutation_set),
            "hunks": [_hunk_payload(record) for record in self.hunks],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "FileTransactionPreparedEvent":
        _require_keys(
            payload,
            {"mutation_set", "hunks"},
        )
        if type(payload["hunks"]) is not list:
            raise ValueError("prepared transaction hunks are invalid")
        return cls(
            mutation_set=mutation_set_from_dict(payload["mutation_set"]),
            hunks=tuple(_hunk_from_payload(item) for item in payload["hunks"]),
        )


@dataclass(frozen=True)
class FileTransactionCommittedEvent:
    transaction_id: str
    versions: tuple[FileVersion, ...]

    type: ClassVar[str] = FILE_TRANSACTION_COMMITTED

    def payload(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "versions": [version_to_dict(version) for version in self.versions],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "FileTransactionCommittedEvent":
        _require_keys(payload, {"transaction_id", "versions"})
        if type(payload["versions"]) is not list or not payload["versions"]:
            raise ValueError("committed transaction versions are invalid")
        return cls(
            transaction_id=_text(payload, "transaction_id", nonempty=True),
            versions=tuple(version_from_dict(item) for item in payload["versions"]),
        )


@dataclass(frozen=True)
class FileTransactionAbortedEvent:
    transaction_id: str
    detail: str = ""

    type: ClassVar[str] = FILE_TRANSACTION_ABORTED

    def payload(self) -> dict[str, Any]:
        return {"transaction_id": self.transaction_id, "detail": self.detail}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "FileTransactionAbortedEvent":
        _require_keys(payload, {"transaction_id", "detail"})
        return cls(
            transaction_id=_text(payload, "transaction_id", nonempty=True),
            detail=_text(payload, "detail"),
        )


@dataclass(frozen=True)
class FileTransactionInDoubtEvent:
    transaction_id: str
    detail: str

    type: ClassVar[str] = FILE_TRANSACTION_IN_DOUBT

    def payload(self) -> dict[str, Any]:
        return {"transaction_id": self.transaction_id, "detail": self.detail}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "FileTransactionInDoubtEvent":
        _require_keys(payload, {"transaction_id", "detail"})
        return cls(
            transaction_id=_text(payload, "transaction_id", nonempty=True),
            detail=_text(payload, "detail", nonempty=True),
        )


FileTransactionEvent = (
    FileTransactionPreparedEvent
    | FileTransactionCommittedEvent
    | FileTransactionAbortedEvent
    | FileTransactionInDoubtEvent
)


@dataclass(frozen=True)
class FileHistoryImportedEvent:
    """Verified before-image imported from a pre-transaction rollout."""

    import_id: str
    source_ordinal: int
    recorded_at: str
    path: str
    display_path: str
    operation: str
    before: BlobRef | None
    source: str
    source_schema_version: int

    type: ClassVar[str] = FILE_HISTORY_IMPORTED

    def __post_init__(self) -> None:
        if (
            type(self.import_id) is not str
            or len(self.import_id) != 64
            or any(character not in "0123456789abcdef" for character in self.import_id)
        ):
            raise ValueError("imported file history id is invalid")
        if type(self.source_ordinal) is not int or self.source_ordinal < 1:
            raise ValueError("imported file history source ordinal is invalid")
        for name, value in (
            ("recorded_at", self.recorded_at),
            ("path", self.path),
            ("display_path", self.display_path),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"imported file history {name} is invalid")
        if self.operation not in {"create", "update"}:
            raise ValueError("imported file history operation is invalid")
        if (self.operation == "create") != (self.before is None):
            raise ValueError("imported file history before-image is invalid")
        if self.before is not None and type(self.before) is not BlobRef:
            raise TypeError("imported file history before-image is invalid")
        if type(self.source) is not str:
            raise ValueError("imported file history source is invalid")
        if type(self.source_schema_version) is not int or self.source_schema_version < 1:
            raise ValueError("imported file history source schema is invalid")

    def payload(self) -> dict[str, Any]:
        return {
            "import_id": self.import_id,
            "source_ordinal": self.source_ordinal,
            "recorded_at": self.recorded_at,
            "path": self.path,
            "display_path": self.display_path,
            "operation": self.operation,
            "before": blob_to_dict(self.before),
            "source": self.source,
            "source_schema_version": self.source_schema_version,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "FileHistoryImportedEvent":
        _require_keys(
            payload,
            {
                "import_id",
                "source_ordinal",
                "recorded_at",
                "path",
                "display_path",
                "operation",
                "before",
                "source",
                "source_schema_version",
            },
        )
        operation = _text(payload, "operation", nonempty=True)
        before = blob_from_dict(payload["before"])
        import_id = _text(payload, "import_id", nonempty=True)
        return cls(
            import_id=import_id,
            source_ordinal=_integer(payload, "source_ordinal", minimum=1),
            recorded_at=_text(payload, "recorded_at", nonempty=True),
            path=_text(payload, "path", nonempty=True),
            display_path=_text(payload, "display_path", nonempty=True),
            operation=operation,
            before=before,
            source=_text(payload, "source"),
            source_schema_version=_integer(
                payload,
                "source_schema_version",
                minimum=1,
            ),
        )


@dataclass(frozen=True)
class FileEditPlanStoredEvent:
    plan_id: str
    manifest: BlobRef

    type: ClassVar[str] = FILE_EDIT_PLAN_STORED

    def payload(self) -> dict[str, Any]:
        return {"plan_id": self.plan_id, "manifest": blob_to_dict(self.manifest)}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "FileEditPlanStoredEvent":
        _require_keys(payload, {"plan_id", "manifest"})
        manifest = blob_from_dict(payload["manifest"])
        if manifest is None:
            raise ValueError("edit plan manifest is missing")
        return cls(
            plan_id=_text(payload, "plan_id", nonempty=True),
            manifest=manifest,
        )


@dataclass(frozen=True)
class HunkDetectedEvent:
    record: HunkRecord

    type: ClassVar[str] = HUNK_DETECTED

    def payload(self) -> dict[str, Any]:
        return {"record": _hunk_payload(self.record)}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "HunkDetectedEvent":
        _require_keys(payload, {"record"})
        return cls(record=_hunk_from_payload(payload["record"]))


@dataclass(frozen=True)
class HunkReviewTransitionedEvent:
    hunk_id: str
    expected_version: int
    version: int
    status: ReviewStatus
    new_range: tuple[int, int]
    post_hash: str
    expected_digest: str
    child_transaction_id: str = ""

    type: ClassVar[str] = HUNK_REVIEW_TRANSITIONED

    def payload(self) -> dict[str, Any]:
        return {
            "hunk_id": self.hunk_id,
            "expected_version": self.expected_version,
            "version": self.version,
            "status": self.status.value,
            "new_range": list(self.new_range),
            "post_hash": self.post_hash,
            "expected_digest": self.expected_digest,
            "child_transaction_id": self.child_transaction_id,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "HunkReviewTransitionedEvent":
        _require_keys(
            payload,
            {
                "hunk_id",
                "expected_version",
                "version",
                "status",
                "new_range",
                "post_hash",
                "expected_digest",
                "child_transaction_id",
            },
        )
        return cls(
            hunk_id=_text(payload, "hunk_id", nonempty=True),
            expected_version=_integer(payload, "expected_version", minimum=1),
            version=_integer(payload, "version", minimum=2),
            status=ReviewStatus(_text(payload, "status", nonempty=True)),
            new_range=_range(payload, "new_range"),
            post_hash=_text(payload, "post_hash", nonempty=True),
            expected_digest=_text(payload, "expected_digest", nonempty=True),
            child_transaction_id=_text(payload, "child_transaction_id"),
        )


FileReviewEvent = HunkDetectedEvent | HunkReviewTransitionedEvent


@dataclass(frozen=True)
class RewindPreparedEvent:
    transaction_id: str
    session_id: str
    project_identity: ProjectIdentity
    working_dir: str
    safety_commit: str
    target_commit: str
    prompt_index: int
    source_epoch: int
    external_paths: tuple[str, ...] = ()

    type: ClassVar[str] = REWIND_PREPARED

    def payload(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "session_id": self.session_id,
            "project_identity": {
                "key": self.project_identity.key,
                "scheme": self.project_identity.scheme,
            },
            "working_dir": self.working_dir,
            "safety_commit": self.safety_commit,
            "target_commit": self.target_commit,
            "prompt_index": self.prompt_index,
            "source_epoch": self.source_epoch,
            "external_paths": list(self.external_paths),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RewindPreparedEvent":
        _require_keys(
            payload,
            {
                "transaction_id",
                "session_id",
                "project_identity",
                "working_dir",
                "safety_commit",
                "target_commit",
                "prompt_index",
                "source_epoch",
                "external_paths",
            },
        )
        project = payload["project_identity"]
        if type(project) is not dict or set(project) != {"key", "scheme"}:
            raise ValueError("rewind project identity is invalid")
        if type(project["key"]) is not str or type(project["scheme"]) is not str:
            raise ValueError("rewind project identity fields are invalid")
        return cls(
            transaction_id=_text(payload, "transaction_id", nonempty=True),
            session_id=_text(payload, "session_id", nonempty=True),
            project_identity=ProjectIdentity(
                key=project["key"],
                scheme=project["scheme"],
            ),
            working_dir=_text(payload, "working_dir", nonempty=True),
            safety_commit=_text(payload, "safety_commit", nonempty=True),
            target_commit=_text(payload, "target_commit", nonempty=True),
            prompt_index=_integer(payload, "prompt_index"),
            source_epoch=_integer(payload, "source_epoch", minimum=0),
            external_paths=_string_list(payload, "external_paths"),
        )


@dataclass(frozen=True)
class RewindCommittedEvent:
    transaction_id: str
    source_epoch: int
    target_epoch: int

    def __post_init__(self) -> None:
        if type(self.source_epoch) is not int or self.source_epoch < 0:
            raise ValueError("rewind source epoch must be a non-negative integer")
        if type(self.target_epoch) is not int or self.target_epoch != self.source_epoch + 1:
            raise ValueError("rewind target epoch must immediately follow source epoch")

    type: ClassVar[str] = REWIND_COMMITTED

    def payload(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "source_epoch": self.source_epoch,
            "target_epoch": self.target_epoch,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RewindCommittedEvent":
        _require_keys(payload, {"transaction_id", "source_epoch", "target_epoch"})
        return cls(
            transaction_id=_text(payload, "transaction_id", nonempty=True),
            source_epoch=_integer(payload, "source_epoch", minimum=0),
            target_epoch=_integer(payload, "target_epoch", minimum=1),
        )


@dataclass(frozen=True)
class RewindAbortedEvent:
    transaction_id: str
    detail: str = ""

    type: ClassVar[str] = REWIND_ABORTED

    def payload(self) -> dict[str, Any]:
        return {"transaction_id": self.transaction_id, "detail": self.detail}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RewindAbortedEvent":
        _require_keys(payload, {"transaction_id", "detail"})
        return cls(
            transaction_id=_text(payload, "transaction_id", nonempty=True),
            detail=_text(payload, "detail"),
        )


@dataclass(frozen=True)
class RewindInDoubtEvent:
    transaction_id: str
    detail: str

    type: ClassVar[str] = REWIND_IN_DOUBT

    def payload(self) -> dict[str, Any]:
        return {"transaction_id": self.transaction_id, "detail": self.detail}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RewindInDoubtEvent":
        _require_keys(payload, {"transaction_id", "detail"})
        return cls(
            transaction_id=_text(payload, "transaction_id", nonempty=True),
            detail=_text(payload, "detail", nonempty=True),
        )


RewindEvent = RewindPreparedEvent | RewindCommittedEvent | RewindAbortedEvent | RewindInDoubtEvent
FileOperationsEvent = (
    FileTransactionEvent | FileHistoryImportedEvent | FileEditPlanStoredEvent | FileReviewEvent | RewindEvent
)


__all__ = [
    "FILE_TRANSACTION_ABORTED",
    "FILE_TRANSACTION_COMMITTED",
    "FILE_TRANSACTION_IN_DOUBT",
    "FILE_TRANSACTION_PREPARED",
    "FILE_HISTORY_IMPORTED",
    "FILE_EDIT_PLAN_STORED",
    "HUNK_DETECTED",
    "HUNK_REVIEW_TRANSITIONED",
    "REWIND_ABORTED",
    "REWIND_COMMITTED",
    "REWIND_IN_DOUBT",
    "REWIND_PREPARED",
    "FileOperationsEvent",
    "FileHistoryImportedEvent",
    "FileEditPlanStoredEvent",
    "FileReviewEvent",
    "FileTransactionAbortedEvent",
    "FileTransactionCommittedEvent",
    "FileTransactionEvent",
    "FileTransactionInDoubtEvent",
    "FileTransactionPreparedEvent",
    "HunkDetectedEvent",
    "HunkReviewTransitionedEvent",
    "RewindAbortedEvent",
    "RewindCommittedEvent",
    "RewindEvent",
    "RewindInDoubtEvent",
    "RewindPreparedEvent",
]
