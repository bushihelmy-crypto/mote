"""Typed hook invocations shared across layer boundaries."""

from dataclasses import dataclass, field
from typing import TypeAlias

from mote.contracts.authorization import PermissionMode


@dataclass(frozen=True, slots=True)
class HookIdentity:
    session_id: str = ""
    cwd: str = ""
    transcript_path: str = ""


@dataclass(frozen=True, slots=True)
class PreToolUsePayload:
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_use_id: str | None = None


@dataclass(frozen=True, slots=True)
class PostToolUsePayload:
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_response: str = ""
    success: bool = False
    error: dict | None = None
    tool_use_id: str | None = None


@dataclass(frozen=True, slots=True)
class UserPromptSubmitPayload:
    prompt: str = ""


@dataclass(frozen=True, slots=True)
class SessionStartPayload:
    source: str = ""


@dataclass(frozen=True, slots=True)
class StopPayload:
    pass


@dataclass(frozen=True, slots=True)
class CompactPayload:
    trigger: str = ""
    compact_summary: str = ""


@dataclass(frozen=True, slots=True)
class FileChangedPayload:
    path: str = ""
    change_type: str = ""
    prior_version: object = None
    version: object = None
    attribution: object = None


@dataclass(frozen=True, slots=True)
class PreToolUseInvocation:
    identity: HookIdentity
    permission_mode: PermissionMode | None
    payload: PreToolUsePayload


@dataclass(frozen=True, slots=True)
class PostToolUseInvocation:
    identity: HookIdentity
    payload: PostToolUsePayload


@dataclass(frozen=True, slots=True)
class UserPromptSubmitInvocation:
    identity: HookIdentity
    payload: UserPromptSubmitPayload


@dataclass(frozen=True, slots=True)
class SessionStartInvocation:
    identity: HookIdentity
    payload: SessionStartPayload


@dataclass(frozen=True, slots=True)
class StopInvocation:
    identity: HookIdentity
    payload: StopPayload


@dataclass(frozen=True, slots=True)
class PreCompactInvocation:
    identity: HookIdentity
    payload: CompactPayload


@dataclass(frozen=True, slots=True)
class PostCompactInvocation:
    identity: HookIdentity
    payload: CompactPayload


@dataclass(frozen=True, slots=True)
class FileChangedInvocation:
    identity: HookIdentity
    payload: FileChangedPayload


HookInvocation: TypeAlias = (
    PreToolUseInvocation
    | PostToolUseInvocation
    | UserPromptSubmitInvocation
    | SessionStartInvocation
    | StopInvocation
    | PreCompactInvocation
    | PostCompactInvocation
    | FileChangedInvocation
)


__all__ = [name for name in globals() if name.endswith(("Invocation", "Payload"))] + ["HookIdentity"]
