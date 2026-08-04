"""Versioned external JSON adapter for typed hook invocations."""

from mote.contracts.events.envelope import JsonValue, thaw_json
from mote.contracts.file.codec import version_from_dict, version_to_dict
from mote.contracts.file.identity import FileChangeAttribution, FileChangeKind
from mote.contracts.hook import (
    FileChangedInvocation,
    FileChangedPayload,
    HookInvocation,
    PostCompactInvocation,
    PostToolUseInvocation,
    PreCompactInvocation,
    PreToolUseInvocation,
    SessionStartInvocation,
    StopInvocation,
    UserPromptSubmitInvocation,
)


class HookWireSerializer:
    def to_json_dict(self, invocation: HookInvocation) -> dict:
        event = self._event_name(invocation)
        identity = invocation.identity
        wire = self._payload(invocation)
        wire.update(
            {
                "schema_version": invocation.schema_version,
                "kind": invocation.kind,
                "hook_event_name": event,
                "hookEventName": event,
                "session_id": identity.session_id,
                "sessionId": identity.session_id,
                "cwd": identity.cwd,
                "transcript_path": identity.transcript_path,
                "transcriptPath": identity.transcript_path,
            }
        )
        if isinstance(invocation, PreToolUseInvocation):
            mode = invocation.permission_mode
            if mode is not None:
                wire["permission_mode"] = mode
                wire["permissionMode"] = mode
        return wire

    @staticmethod
    def _event_name(invocation: HookInvocation) -> str:
        if isinstance(invocation, PreToolUseInvocation):
            return "PreToolUse"
        if isinstance(invocation, PostToolUseInvocation):
            return "PostToolUse"
        if isinstance(invocation, UserPromptSubmitInvocation):
            return "UserPromptSubmit"
        if isinstance(invocation, SessionStartInvocation):
            return "SessionStart"
        if isinstance(invocation, StopInvocation):
            return "Stop"
        if isinstance(invocation, PreCompactInvocation):
            return "PreCompact"
        if isinstance(invocation, PostCompactInvocation):
            return "PostCompact"
        if isinstance(invocation, FileChangedInvocation):
            return "FileChanged"
        raise TypeError("unsupported hook invocation")

    @classmethod
    def _payload(cls, invocation: HookInvocation) -> dict:
        if isinstance(invocation, PreToolUseInvocation):
            payload = invocation.payload
            return {
                "identity": payload.identity.to_payload(),
                "tool_name": payload.tool_name,
                "tool_input": thaw_json(dict(payload.tool_input)),
            }
        if isinstance(invocation, PostToolUseInvocation):
            payload = invocation.payload
            return {
                "identity": payload.identity.to_payload(),
                "tool_name": payload.tool_name,
                "tool_input": thaw_json(dict(payload.tool_input)),
                "tool_response": payload.tool_response,
                "success": payload.success,
                "error": None if payload.error is None else thaw_json(dict(payload.error)),
            }
        if isinstance(invocation, UserPromptSubmitInvocation):
            return {"prompt": invocation.payload.prompt}
        if isinstance(invocation, SessionStartInvocation):
            return {"source": invocation.payload.source}
        if isinstance(invocation, StopInvocation):
            return {}
        if isinstance(invocation, (PreCompactInvocation, PostCompactInvocation)):
            payload = invocation.payload
            return {"trigger": payload.trigger, "compact_summary": payload.compact_summary}
        if isinstance(invocation, FileChangedInvocation):
            return cls.file_changed_payload_to_json_dict(invocation.payload)
        raise TypeError("unsupported hook invocation")

    @staticmethod
    def file_changed_payload_to_json_dict(payload: FileChangedPayload) -> dict:
        return {
            "path": payload.path,
            "change_type": payload.change_type.value,
            "prior_version": version_to_dict(payload.prior_version),
            "version": version_to_dict(payload.version),
            "attribution": payload.attribution.value,
        }

    @staticmethod
    def file_changed_payload_from_json_dict(data: dict) -> FileChangedPayload:
        if type(data) is not dict or set(data) != {"path", "change_type", "prior_version", "version", "attribution"}:
            raise ValueError("FileChanged payload fields are not canonical")
        path = data["path"]
        change_type = data["change_type"]
        attribution = data["attribution"]
        if type(path) is not str or not path:
            raise ValueError("FileChanged path is invalid")
        if type(change_type) is not str or type(attribution) is not str:
            raise ValueError("FileChanged discriminator is invalid")
        try:
            kind = FileChangeKind(change_type)
            source = FileChangeAttribution(attribution)
        except ValueError as exc:
            raise ValueError("FileChanged discriminator is unknown") from exc
        return FileChangedPayload(
            path=path,
            change_type=kind,
            prior_version=version_from_dict(data["prior_version"]),
            version=version_from_dict(data["version"]),
            attribution=source,
        )


__all__ = ["HookWireSerializer"]
