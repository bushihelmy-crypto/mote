"""Versioned external JSON adapter for typed hook invocations."""

from dataclasses import asdict

from mote.contracts.file.codec import version_from_dict, version_to_dict
from mote.contracts.file.identity import FileChangeAttribution, FileChangeKind
from mote.contracts.hook import FileChangedInvocation, FileChangedPayload, HookInvocation, PreToolUseInvocation

_EVENT_NAMES = {
    "PreToolUseInvocation": "PreToolUse",
    "PostToolUseInvocation": "PostToolUse",
    "UserPromptSubmitInvocation": "UserPromptSubmit",
    "SessionStartInvocation": "SessionStart",
    "StopInvocation": "Stop",
    "PreCompactInvocation": "PreCompact",
    "PostCompactInvocation": "PostCompact",
    "FileChangedInvocation": "FileChanged",
}


class HookWireSerializer:
    def to_json_dict(self, invocation: HookInvocation) -> dict:
        event = _EVENT_NAMES[type(invocation).__name__]
        identity = invocation.identity
        wire = (
            self.file_changed_payload_to_json_dict(invocation.payload)
            if isinstance(invocation, FileChangedInvocation)
            else asdict(invocation.payload)
        )
        wire.update(
            {
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
