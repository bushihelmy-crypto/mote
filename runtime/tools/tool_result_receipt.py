"""Strict v2 durable codec for exact tool-result replay."""

from __future__ import annotations

import base64
import binascii
import json

from mote.contracts.artifact import ArtifactRef, ArtifactRetention, ArtifactSensitivity
from mote.contracts.async_work.submission import decode_async_work_submission, encode_async_work_submission
from mote.contracts.events.envelope import freeze_json, thaw_json
from mote.contracts.foundation.errors.report import ErrorReport
from mote.contracts.tool.result import (
    ArtifactToolPayload,
    FileChange,
    InlineBinaryToolPayload,
    JsonToolPayload,
    ToolMedia,
    ToolPayload,
)
from mote.runtime.tools.tool_result import ToolResult

_CODEC = "tool-result+json@4"
_RESULT_KEYS = frozenset(
    {
        "output",
        "success",
        "payload",
        "media",
        "artifacts",
        "file_changes",
        "error",
        "terminate",
        "retention",
        "resource_path",
        "async_work_submission",
    }
)
_ARTIFACT_KEYS = frozenset(
    {
        "artifact_id",
        "revision",
        "representation",
        "kind",
        "mime_type",
        "content_ref",
        "digest",
        "size",
        "retention",
        "sensitivity",
        "suggested_name",
    }
)
_MEDIA_KEYS = frozenset({"artifact", "kind", "ref", "mime"})
_FILE_CHANGE_KEYS = frozenset({"path", "old", "new", "transaction_id", "post_digest"})
_ERROR_KEYS = frozenset(
    {"schema", "namespace", "error", "code", "message", "retryable", "recovery", "context", "cause"}
)


def encode_tool_result_receipt(result: ToolResult) -> str:
    envelope = {
        "codec": _CODEC,
        "result": {
            "output": result.output,
            "success": result.success,
            "payload": _payload_to_dict(result.payload),
            "media": [
                {
                    "artifact": _artifact_to_dict(item.artifact),
                    "kind": item.kind,
                    "ref": item.ref,
                    "mime": item.mime,
                }
                for item in result.media
            ],
            "artifacts": [_artifact_to_dict(item) for item in result.artifacts],
            "file_changes": [
                {
                    "path": item.path,
                    "old": item.old,
                    "new": item.new,
                    "transaction_id": item.transaction_id,
                    "post_digest": item.post_digest,
                }
                for item in result.file_changes
            ],
            "error": _error_to_dict(result.error),
            "terminate": result.terminate,
            "retention": result.retention,
            "resource_path": result.resource_path,
            "async_work_submission": (
                None
                if result.async_work_submission is None
                else encode_async_work_submission(result.async_work_submission)
            ),
        },
    }
    return json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def decode_tool_result_receipt(payload: str | None, *, success: bool) -> ToolResult:
    if payload is None:
        return ToolResult(output="", success=success)
    try:
        envelope = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("tool result receipt is not valid JSON") from error
    envelope = _object(envelope, {"codec", "result"}, "tool result envelope")
    if envelope["codec"] != _CODEC:
        raise ValueError("unsupported tool result receipt codec")
    raw = envelope["result"]
    raw = _object(raw, _RESULT_KEYS, "tool result")
    output = _string(raw["output"], "tool result output")
    recorded_success = _boolean(raw["success"], "tool result success")
    if recorded_success is not success:
        raise ValueError("tool result success disagrees with journal terminal state")
    return ToolResult(
        output=output,
        success=recorded_success,
        payload=_payload_from_dict(raw["payload"]),
        media=tuple(_media_from_dict(item) for item in _list(raw["media"], "tool result media")),
        artifacts=tuple(_artifact_from_dict(item) for item in _list(raw["artifacts"], "tool result artifacts")),
        file_changes=tuple(
            _file_change_from_dict(item) for item in _list(raw["file_changes"], "tool result file changes")
        ),
        error=_error_from_dict(raw["error"]),
        terminate=_boolean(raw["terminate"], "tool result terminate"),
        retention=_optional_string(raw["retention"], "tool result retention"),
        resource_path=_optional_string(raw["resource_path"], "tool result resource path"),
        async_work_submission=(
            None if raw["async_work_submission"] is None else decode_async_work_submission(raw["async_work_submission"])
        ),
    )


def _payload_to_dict(payload: ToolPayload | None) -> dict[str, object] | None:
    if payload is None:
        return None
    if isinstance(payload, JsonToolPayload):
        return {"kind": "json", "value": thaw_json(payload.value)}
    if isinstance(payload, InlineBinaryToolPayload):
        return {
            "kind": "inline_binary",
            "base64": base64.b64encode(payload.value).decode("ascii"),
        }
    if isinstance(payload, ArtifactToolPayload):
        return {"kind": "artifact", "reference": _artifact_to_dict(payload.reference)}
    raise TypeError(f"unregistered durable tool payload: {type(payload).__name__}")


def _payload_from_dict(raw: object) -> ToolPayload | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or type(raw.get("kind")) is not str:
        raise ValueError("tool result payload must be a tagged object")
    kind = raw["kind"]
    if kind == "json":
        raw = _object(raw, {"kind", "value"}, "JSON tool payload")
        return JsonToolPayload(freeze_json(raw["value"], path="tool_result.payload"))
    if kind == "inline_binary":
        raw = _object(raw, {"kind", "base64"}, "binary tool payload")
        encoded = _string(raw["base64"], "binary tool payload base64")
        try:
            return InlineBinaryToolPayload(base64.b64decode(encoded, validate=True))
        except (ValueError, binascii.Error) as error:
            raise ValueError("invalid binary tool payload base64") from error
    if kind == "artifact":
        raw = _object(raw, {"kind", "reference"}, "artifact tool payload")
        return ArtifactToolPayload(_artifact_from_dict(raw["reference"]))
    raise ValueError(f"unknown tool result payload kind: {kind}")


def _artifact_to_dict(ref: ArtifactRef) -> dict[str, object]:
    return {
        "artifact_id": ref.artifact_id,
        "revision": ref.revision,
        "representation": ref.representation,
        "kind": ref.kind,
        "mime_type": ref.mime_type,
        "content_ref": ref.content_ref,
        "digest": ref.digest,
        "size": ref.size,
        "retention": ref.retention.value,
        "sensitivity": ref.sensitivity.value,
        "suggested_name": ref.suggested_name,
    }


def _artifact_from_dict(raw: object) -> ArtifactRef:
    raw = _object(raw, _ARTIFACT_KEYS, "artifact reference")
    return ArtifactRef(
        artifact_id=_string(raw["artifact_id"], "artifact id"),
        revision=_integer(raw["revision"], "artifact revision"),
        representation=_string(raw["representation"], "artifact representation"),
        kind=_string(raw["kind"], "artifact kind"),
        mime_type=_string(raw["mime_type"], "artifact mime type"),
        content_ref=_string(raw["content_ref"], "artifact content ref"),
        digest=_string(raw["digest"], "artifact digest"),
        size=_integer(raw["size"], "artifact size"),
        retention=ArtifactRetention(_string(raw["retention"], "artifact retention")),
        sensitivity=ArtifactSensitivity(_string(raw["sensitivity"], "artifact sensitivity")),
        suggested_name=_string(raw["suggested_name"], "artifact suggested name", allow_empty=True),
    )


def _media_from_dict(raw: object) -> ToolMedia:
    raw = _object(raw, _MEDIA_KEYS, "tool media")
    return ToolMedia(
        artifact=_artifact_from_dict(raw["artifact"]),
        kind=_string(raw["kind"], "tool media kind"),
        ref=_string(raw["ref"], "tool media ref", allow_empty=True),
        mime=_optional_string(raw["mime"], "tool media mime"),
    )


def _file_change_from_dict(raw: object) -> FileChange:
    raw = _object(raw, _FILE_CHANGE_KEYS, "file change")
    return FileChange(**{key: _string(raw[key], f"file change {key}", allow_empty=True) for key in _FILE_CHANGE_KEYS})


def _error_to_dict(error: ErrorReport | None) -> dict[str, object] | None:
    if error is None:
        return None
    return error.as_dict()


def _error_from_dict(raw: object) -> ErrorReport | None:
    if raw is None:
        return None
    raw = _object(raw, _ERROR_KEYS, "tool error")
    return ErrorReport.from_dict(raw)


def _object(value: object, keys: set[str] | frozenset[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{label} has an invalid shape")
    return value


def _string(value: object, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise ValueError(f"{label} must be {'a string' if allow_empty else 'a non-empty string'}")
    return value


def _optional_string(value: object, label: str) -> str | None:
    return None if value is None else _string(value, label, allow_empty=False)


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


__all__ = ["decode_tool_result_receipt", "encode_tool_result_receipt"]
