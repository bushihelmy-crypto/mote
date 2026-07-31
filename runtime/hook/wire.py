"""Versioned external JSON adapter for typed hook invocations."""

from dataclasses import asdict

from mote.contracts.hook import HookInvocation, PreToolUseInvocation

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
        wire = asdict(invocation.payload)
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


__all__ = ["HookWireSerializer"]
