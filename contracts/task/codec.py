"""Strict wire codec for canonical BackgroundTask result pointers."""

from mote.contracts.artifact import ArtifactRef, ArtifactRetention, ArtifactSensitivity
from mote.contracts.task.models import (
    CommandName,
    CompletedArtifactTaskResultPointer,
    CompletedInlineTaskResultPointer,
    FailedTaskResultPointer,
    InlineTaskOutput,
    TaskFailure,
    TaskId,
    TaskResultPointer,
)


def encode_task_result_pointer(value: TaskResultPointer) -> dict[str, object]:
    common: dict[str, object] = {
        "task_id": str(value.task_id),
        "command_name": str(value.command_name),
        "summary": value.summary,
    }
    if isinstance(value, CompletedInlineTaskResultPointer):
        return {"kind": "completed_inline", **common, "output": value.output.content}
    if isinstance(value, CompletedArtifactTaskResultPointer):
        artifact = value.output
        return {
            "kind": "completed_artifact",
            **common,
            "output": {
                "artifact_id": artifact.artifact_id,
                "revision": artifact.revision,
                "representation": artifact.representation,
                "artifact_kind": artifact.kind,
                "mime_type": artifact.mime_type,
                "content_ref": artifact.content_ref,
                "digest": artifact.digest,
                "size": artifact.size,
                "retention": artifact.retention.value,
                "sensitivity": artifact.sensitivity.value,
                "suggested_name": artifact.suggested_name,
            },
        }
    if isinstance(value, FailedTaskResultPointer):
        return {"kind": "failed", **common, "error": value.error.message}
    raise TypeError("unsupported task result pointer")


def decode_task_result_pointer(raw: object) -> TaskResultPointer:
    if type(raw) is not dict or type(raw.get("kind")) is not str:
        raise ValueError("task result pointer envelope is invalid")
    kind = raw["kind"]
    payload_field = {
        "completed_inline": "output",
        "completed_artifact": "output",
        "failed": "error",
    }.get(kind)
    if payload_field is None or set(raw) != {"kind", "task_id", "command_name", "summary", payload_field}:
        raise ValueError("task result pointer shape is invalid")
    for field in ("task_id", "command_name", "summary"):
        if type(raw[field]) is not str or not raw[field]:
            raise ValueError("task result pointer string is invalid")
    common = (TaskId(raw["task_id"]), CommandName(raw["command_name"]), raw["summary"])
    payload = raw[payload_field]
    if kind == "completed_artifact":
        fields = {
            "artifact_id",
            "revision",
            "representation",
            "artifact_kind",
            "mime_type",
            "content_ref",
            "digest",
            "size",
            "retention",
            "sensitivity",
            "suggested_name",
        }
        if type(payload) is not dict or set(payload) != fields:
            raise ValueError("task artifact result shape is invalid")
        for field in fields - {"revision", "size"}:
            if type(payload[field]) is not str:
                raise ValueError("task artifact result primitive is invalid")
        if type(payload["revision"]) is not int or type(payload["size"]) is not int:
            raise ValueError("task artifact result counter is invalid")
        return CompletedArtifactTaskResultPointer(
            *common,
            ArtifactRef(
                payload["artifact_id"],
                payload["revision"],
                payload["representation"],
                payload["artifact_kind"],
                payload["mime_type"],
                payload["content_ref"],
                payload["digest"],
                payload["size"],
                ArtifactRetention(payload["retention"]),
                ArtifactSensitivity(payload["sensitivity"]),
                payload["suggested_name"],
            ),
        )
    if type(payload) is not str:
        raise ValueError("task result pointer payload is invalid")
    if kind == "completed_inline":
        return CompletedInlineTaskResultPointer(*common, InlineTaskOutput(payload))
    if kind == "failed":
        return FailedTaskResultPointer(*common, TaskFailure(payload))
    raise ValueError("task result pointer kind is invalid")


__all__ = ["decode_task_result_pointer", "encode_task_result_pointer"]
