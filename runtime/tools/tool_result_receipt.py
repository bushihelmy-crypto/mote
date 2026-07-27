"""Versioned durable codec for exact tool-result replay."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel

from mote.contracts.artifacts import ArtifactRef, ArtifactRetention, ArtifactSensitivity
from mote.contracts.errors.report import ErrorReport
from mote.runtime.tools.tool_result import FileChange, ToolMedia, ToolResult

_CODEC = "tool-result+json@1"
_BYTES_TAG = "$mote.bytes.base64"
_TUPLE_TAG = "$mote.tuple"
_OBJECT_TAG = "$mote.object"


def encode_tool_result_receipt(result: ToolResult) -> str:
    envelope = {
        "codec": _CODEC,
        "result": {
            "output": result.output,
            "success": result.success,
            "data": _encode_value(result.data),
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
            "file_changes": [asdict(item) for item in result.file_changes],
            "error": result.error.as_dict() if result.error is not None else None,
            "terminate": result.terminate,
            "retention": result.retention,
            "resource_path": result.resource_path,
        },
    }
    return json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_tool_result_receipt(payload: str | None, *, success: bool) -> ToolResult:
    if payload is None:
        return ToolResult(output="", success=success)
    try:
        envelope = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return ToolResult(output=payload, success=success)
    if not isinstance(envelope, dict) or envelope.get("codec") != _CODEC:
        return ToolResult(output=payload, success=success)
    raw = envelope.get("result")
    if not isinstance(raw, dict):
        raise ValueError("tool result receipt has no result object")
    return ToolResult(
        output=str(raw.get("output", "")),
        success=bool(raw.get("success", success)),
        data=_decode_value(raw.get("data")),
        media=[
            ToolMedia(
                artifact=_artifact_from_dict(item["artifact"]),
                kind=str(item.get("kind", "image")),
                ref=str(item.get("ref", "")),
                mime=(str(item["mime"]) if item.get("mime") is not None else None),
            )
            for item in raw.get("media", ())
        ],
        artifacts=[_artifact_from_dict(item) for item in raw.get("artifacts", ())],
        file_changes=[FileChange(**item) for item in raw.get("file_changes", ())],
        error=(ErrorReport.from_dict(raw["error"]) if isinstance(raw.get("error"), dict) else None),
        terminate=bool(raw.get("terminate", False)),
        retention=(str(raw["retention"]) if raw.get("retention") is not None else None),
        resource_path=(str(raw["resource_path"]) if raw.get("resource_path") is not None else None),
    )


def _artifact_to_dict(ref: ArtifactRef) -> dict[str, Any]:
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


def _artifact_from_dict(raw: dict[str, Any]) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=str(raw["artifact_id"]),
        revision=int(raw["revision"]),
        representation=str(raw["representation"]),
        kind=str(raw["kind"]),
        mime_type=str(raw["mime_type"]),
        content_ref=str(raw["content_ref"]),
        digest=str(raw["digest"]),
        size=int(raw["size"]),
        retention=ArtifactRetention(raw["retention"]),
        sensitivity=ArtifactSensitivity(raw["sensitivity"]),
        suggested_name=str(raw.get("suggested_name", "")),
    )


def _encode_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {_BYTES_TAG: base64.b64encode(value).decode("ascii")}
    if isinstance(value, Enum):
        return _encode_value(value.value)
    if isinstance(value, BaseModel):
        return _encode_value(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _encode_value(asdict(value))
    if isinstance(value, tuple):
        return {_TUPLE_TAG: [_encode_value(item) for item in value]}
    if isinstance(value, list):
        return [_encode_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode_value(item) for key, item in value.items()}
    return {
        _OBJECT_TAG: {
            "type": f"{type(value).__module__}.{type(value).__qualname__}",
            "repr": repr(value),
        }
    }


def _decode_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {_BYTES_TAG}:
        return base64.b64decode(value[_BYTES_TAG], validate=True)
    if set(value) == {_TUPLE_TAG}:
        return tuple(_decode_value(item) for item in value[_TUPLE_TAG])
    return {key: _decode_value(item) for key, item in value.items()}


__all__ = ["decode_tool_result_receipt", "encode_tool_result_receipt"]
