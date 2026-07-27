"""Stable JSON codecs for File Operations contracts."""

from __future__ import annotations

import base64
import re
from typing import Any

from mote.contracts.fileops.models import (
    AbsentVersion,
    BlobRef,
    CreateMutation,
    DeleteMutation,
    EncodingDecision,
    EncodingSource,
    FileSnapshot,
    FileVersion,
    Mutation,
    MutationKind,
    MutationSet,
    NameIdentity,
    PathToken,
    PresentVersion,
    ProjectIdentity,
    RecoveryPolicy,
    ReplaceMutation,
    SearchRow,
    SearchSkippedFile,
    SearchSkipReason,
    SearchSummary,
    TargetIdentity,
)


def path_to_dict(path: PathToken) -> dict[str, Any]:
    if isinstance(path.native, bytes):
        native = {
            "kind": "bytes",
            "value": base64.b64encode(path.native).decode("ascii"),
        }
    else:
        native = {"kind": "text", "value": path.native}
    return {"display": path.display, "native": native}


def path_from_dict(data: dict[str, Any]) -> PathToken:
    if type(data) is not dict or set(data) != {"display", "native"}:
        raise ValueError("path token fields are not canonical")
    if type(data["display"]) is not str or not data["display"]:
        raise ValueError("path token display is invalid")
    native = data["native"]
    if type(native) is not dict or set(native) != {"kind", "value"}:
        raise ValueError("native path fields are not canonical")
    if native["kind"] == "bytes":
        if type(native["value"]) is not str:
            raise ValueError("native byte path is invalid")
        value = base64.b64decode(native["value"], validate=True)
    elif native["kind"] == "text":
        if type(native["value"]) is not str:
            raise ValueError("native text path is invalid")
        value = native["value"]
    else:
        raise ValueError(f"unknown native path kind: {native['kind']}")
    if not value:
        raise ValueError("native path is empty")
    return PathToken(display=data["display"], native=value)


def blob_to_dict(ref: BlobRef | None) -> dict[str, Any] | None:
    return None if ref is None else {"digest": ref.digest, "size": ref.size}


def blob_from_dict(data: dict[str, Any] | None) -> BlobRef | None:
    if data is None:
        return None
    if type(data) is not dict or set(data) != {"digest", "size"}:
        raise ValueError("blob reference fields are not canonical")
    digest = data["digest"]
    size = data["size"]
    if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("blob reference digest is invalid")
    if type(size) is not int or not 0 <= size < (1 << 63):
        raise ValueError("blob reference size is invalid")
    return BlobRef(digest=digest, size=size)


def version_to_dict(version: FileVersion) -> dict[str, Any]:
    name = {"key": version.name_identity.key, "scheme": version.name_identity.scheme}
    if isinstance(version, AbsentVersion):
        return {"kind": "absent", "name_identity": name}
    return {
        "kind": "present",
        "name_identity": name,
        "target_identity": {
            "key": version.target_identity.key,
            "scheme": version.target_identity.scheme,
        },
        "size": version.size,
        "mtime_ns": version.mtime_ns,
        "digest": version.digest,
        "metadata_digest": version.metadata_digest,
    }


def version_from_dict(data: dict[str, Any]) -> FileVersion:
    if type(data) is not dict or type(data.get("kind")) is not str:
        raise ValueError("file version fields are invalid")
    expected_fields = (
        {"kind", "name_identity"}
        if data["kind"] == "absent"
        else {
            "kind",
            "name_identity",
            "target_identity",
            "size",
            "mtime_ns",
            "digest",
            "metadata_digest",
        }
    )
    if set(data) != expected_fields:
        raise ValueError("file version fields are not canonical")
    name_data = data["name_identity"]
    name = _name_identity_from_dict(name_data)
    if data["kind"] == "absent":
        return AbsentVersion(name_identity=name)
    if data["kind"] != "present":
        raise ValueError(f"unknown file version kind: {data['kind']}")
    target_data = data["target_identity"]
    _require_identity(target_data, "target")
    size = data["size"]
    mtime_ns = data["mtime_ns"]
    digest = data["digest"]
    metadata_digest = data["metadata_digest"]
    if type(size) is not int or not 0 <= size < (1 << 63):
        raise ValueError("file version size is invalid")
    if type(mtime_ns) is not int or mtime_ns < 0:
        raise ValueError("file version mtime is invalid")
    for label, value in (("digest", digest), ("metadata digest", metadata_digest)):
        if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"file version {label} is invalid")
    return PresentVersion(
        name_identity=name,
        target_identity=TargetIdentity(key=target_data["key"], scheme=target_data["scheme"]),
        size=size,
        mtime_ns=mtime_ns,
        digest=digest,
        metadata_digest=metadata_digest,
    )


def encoding_to_dict(decision: EncodingDecision | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "label": decision.label,
        "bom": base64.b64encode(decision.bom).decode("ascii"),
        "source": decision.source.value,
        "confidence": decision.confidence,
    }


def encoding_from_dict(data: dict[str, Any] | None) -> EncodingDecision | None:
    if data is None:
        return None
    if type(data) is not dict or set(data) != {"label", "bom", "source", "confidence"}:
        raise ValueError("encoding decision fields are not canonical")
    if type(data["label"]) is not str or not data["label"]:
        raise ValueError("encoding label is invalid")
    if type(data["bom"]) is not str or type(data["source"]) is not str:
        raise ValueError("encoding decision is invalid")
    raw_confidence = data["confidence"]
    if raw_confidence is not None and (
        type(raw_confidence) not in (int, float) or isinstance(raw_confidence, bool) or not 0.0 <= raw_confidence <= 1.0
    ):
        raise ValueError("encoding confidence is invalid")
    return EncodingDecision(
        label=data["label"],
        bom=base64.b64decode(data["bom"], validate=True),
        source=EncodingSource(data["source"]),
        confidence=None if raw_confidence is None else float(raw_confidence),
    )


def snapshot_to_dict(snapshot: FileSnapshot) -> dict[str, Any]:
    return {
        "requested_path": path_to_dict(snapshot.requested_path),
        "target_path": path_to_dict(snapshot.target_path),
        "project_identity": {
            "key": snapshot.project_identity.key,
            "scheme": snapshot.project_identity.scheme,
        },
        "version": version_to_dict(snapshot.version),
        "artifact": blob_to_dict(snapshot.artifact),
        "metadata": blob_to_dict(snapshot.metadata),
        "encoding": encoding_to_dict(snapshot.encoding),
    }


def snapshot_from_dict(data: dict[str, Any]) -> FileSnapshot:
    if type(data) is not dict or set(data) != {
        "requested_path",
        "target_path",
        "project_identity",
        "version",
        "artifact",
        "metadata",
        "encoding",
    }:
        raise ValueError("file snapshot fields are not canonical")
    version = version_from_dict(data["version"])
    if not isinstance(version, PresentVersion):
        raise ValueError("file snapshot contains an absent version")
    artifact = blob_from_dict(data["artifact"])
    if artifact is None:
        raise ValueError("file snapshot artifact is missing")
    metadata = blob_from_dict(data["metadata"])
    if metadata is None:
        raise ValueError("file snapshot metadata artifact is missing")
    project = data["project_identity"]
    _require_identity(project, "project")
    return FileSnapshot(
        requested_path=path_from_dict(data["requested_path"]),
        target_path=path_from_dict(data["target_path"]),
        project_identity=ProjectIdentity(
            key=project["key"],
            scheme=project["scheme"],
        ),
        version=version,
        artifact=artifact,
        metadata=metadata,
        encoding=encoding_from_dict(data["encoding"]),
    )


def mutation_to_dict(mutation: Mutation) -> dict[str, Any]:
    if isinstance(mutation, CreateMutation):
        return {
            "kind": mutation.kind.value,
            "requested_path": path_to_dict(mutation.requested_path),
            "target_path": path_to_dict(mutation.target_path),
            "project_identity": {
                "key": mutation.project_identity.key,
                "scheme": mutation.project_identity.scheme,
            },
            "expected_version": version_to_dict(mutation.expected_version),
            "after": blob_to_dict(mutation.after),
            "metadata": blob_to_dict(mutation.metadata),
        }
    if isinstance(mutation, ReplaceMutation):
        return {
            "kind": mutation.kind.value,
            "before": snapshot_to_dict(mutation.before),
            "after": blob_to_dict(mutation.after),
        }
    return {
        "kind": mutation.kind.value,
        "before": snapshot_to_dict(mutation.before),
    }


def mutation_from_dict(data: dict[str, Any]) -> Mutation:
    if type(data) is not dict or type(data.get("kind")) is not str:
        raise ValueError("mutation fields are invalid")
    kind = MutationKind(data["kind"])
    if kind == MutationKind.CREATE:
        if set(data) != {
            "kind",
            "requested_path",
            "target_path",
            "project_identity",
            "expected_version",
            "after",
            "metadata",
        }:
            raise ValueError("create mutation fields are not canonical")
        project = data["project_identity"]
        expected = version_from_dict(data["expected_version"])
        after = blob_from_dict(data["after"])
        metadata = blob_from_dict(data["metadata"])
        if not isinstance(expected, AbsentVersion) or after is None or metadata is None:
            raise ValueError("invalid create mutation")
        _require_identity(project, "project")
        return CreateMutation(
            requested_path=path_from_dict(data["requested_path"]),
            target_path=path_from_dict(data["target_path"]),
            project_identity=ProjectIdentity(key=project["key"], scheme=project["scheme"]),
            expected_version=expected,
            after=after,
            metadata=metadata,
        )
    if kind == MutationKind.REPLACE:
        if set(data) != {"kind", "before", "after"}:
            raise ValueError("replace mutation fields are not canonical")
        before = snapshot_from_dict(data["before"])
        after = blob_from_dict(data["after"])
        if after is None:
            raise ValueError("invalid replace mutation")
        return ReplaceMutation(
            before=before,
            after=after,
        )
    if set(data) != {"kind", "before"}:
        raise ValueError("delete mutation fields are not canonical")
    before = snapshot_from_dict(data["before"])
    return DeleteMutation(
        before=before,
    )


def mutation_set_to_dict(mutation_set: MutationSet) -> dict[str, Any]:
    return {
        "transaction_id": mutation_set.transaction_id,
        "session_id": mutation_set.session_id,
        "source": mutation_set.source,
        "recovery_policy": mutation_set.recovery_policy.value,
        "mutations": [mutation_to_dict(mutation) for mutation in mutation_set.mutations],
    }


def mutation_set_from_dict(data: dict[str, Any]) -> MutationSet:
    if type(data) is not dict or set(data) != {
        "transaction_id",
        "session_id",
        "source",
        "recovery_policy",
        "mutations",
    }:
        raise ValueError("mutation set fields are not canonical")
    for field in ("transaction_id", "session_id", "source", "recovery_policy"):
        if type(data[field]) is not str or not data[field]:
            raise ValueError(f"mutation set {field} is invalid")
    if type(data["mutations"]) is not list or not data["mutations"]:
        raise ValueError("mutation set mutations are invalid")
    mutation_set = MutationSet(
        transaction_id=data["transaction_id"],
        session_id=data["session_id"],
        source=data["source"],
        recovery_policy=RecoveryPolicy(data["recovery_policy"]),
        mutations=tuple(mutation_from_dict(item) for item in data["mutations"]),
    )
    if [mutation_to_dict(item) for item in mutation_set.mutations] != data["mutations"]:
        raise ValueError("mutation set mutations are not in canonical order")
    return mutation_set


def _require_identity(data: Any, label: str) -> None:
    if type(data) is not dict or set(data) != {"key", "scheme"}:
        raise ValueError(f"{label} identity fields are not canonical")
    if type(data["key"]) is not str or not data["key"]:
        raise ValueError(f"{label} identity key is invalid")
    if type(data["scheme"]) is not str or not data["scheme"]:
        raise ValueError(f"{label} identity scheme is invalid")


def _name_identity_from_dict(data: Any) -> NameIdentity:
    _require_identity(data, "name")
    return NameIdentity(key=data["key"], scheme=data["scheme"])


def search_row_to_dict(row: SearchRow) -> dict[str, Any]:
    return {
        "path": path_to_dict(row.path),
        "version": None if row.version is None else version_to_dict(row.version),
        "line_number": row.line_number,
        "text": row.text,
        "matched_text": row.matched_text,
        "occurrence_count": row.occurrence_count,
        "is_context": row.is_context,
    }


def search_row_from_dict(data: dict[str, Any]) -> SearchRow:
    raw_version = data.get("version")
    version = None if raw_version is None else version_from_dict(raw_version)
    if version is not None and not isinstance(version, PresentVersion):
        raise ValueError("search row contains an absent version")
    raw_line = data.get("line_number")
    return SearchRow(
        path=path_from_dict(data["path"]),
        version=version,
        line_number=None if raw_line is None else int(raw_line),
        text=str(data.get("text", "")),
        matched_text=str(data.get("matched_text", "")),
        occurrence_count=int(data.get("occurrence_count", 0)),
        is_context=bool(data.get("is_context", False)),
    )


def search_skipped_to_dict(skipped: SearchSkippedFile) -> dict[str, Any]:
    return {
        "path": path_to_dict(skipped.path),
        "reason": skipped.reason.value,
        "detail": skipped.detail,
    }


def search_skipped_from_dict(data: dict[str, Any]) -> SearchSkippedFile:
    return SearchSkippedFile(
        path=path_from_dict(data["path"]),
        reason=SearchSkipReason(str(data["reason"])),
        detail=str(data.get("detail", "")),
    )


def search_summary_to_dict(summary: SearchSummary) -> dict[str, Any]:
    return {
        "discovered_files": summary.discovered_files,
        "scanned_files": summary.scanned_files,
        "matched_files": summary.matched_files,
        "total_occurrences": summary.total_occurrences,
        "skipped_files": summary.skipped_files,
        "complete": summary.complete,
        "termination": summary.termination,
    }


def search_summary_from_dict(data: dict[str, Any]) -> SearchSummary:
    keys = {
        "discovered_files",
        "scanned_files",
        "matched_files",
        "total_occurrences",
        "skipped_files",
        "complete",
        "termination",
    }
    if type(data) is not dict or set(data) != keys:
        raise ValueError("search summary fields are not canonical")
    for field in (
        "discovered_files",
        "scanned_files",
        "matched_files",
        "total_occurrences",
        "skipped_files",
    ):
        if type(data[field]) is not int or data[field] < 0:
            raise ValueError(f"search summary {field} is invalid")
    if type(data["complete"]) is not bool or type(data["termination"]) is not str:
        raise ValueError("search summary completion fields are invalid")
    return SearchSummary(
        discovered_files=data["discovered_files"],
        scanned_files=data["scanned_files"],
        matched_files=data["matched_files"],
        total_occurrences=data["total_occurrences"],
        skipped_files=data["skipped_files"],
        complete=data["complete"],
        termination=data["termination"],
    )


__all__ = [
    "blob_from_dict",
    "blob_to_dict",
    "mutation_from_dict",
    "mutation_set_from_dict",
    "mutation_set_to_dict",
    "mutation_to_dict",
    "encoding_from_dict",
    "encoding_to_dict",
    "path_from_dict",
    "path_to_dict",
    "search_row_from_dict",
    "search_row_to_dict",
    "search_skipped_from_dict",
    "search_skipped_to_dict",
    "search_summary_from_dict",
    "search_summary_to_dict",
    "snapshot_from_dict",
    "snapshot_to_dict",
    "version_from_dict",
    "version_to_dict",
]
