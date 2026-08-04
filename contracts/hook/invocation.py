"""Typed hook invocations shared across layer boundaries."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, TypeAlias, cast

from mote.contracts.authorization import PermissionMode
from mote.contracts.events.envelope import JsonValue, freeze_json
from mote.contracts.file.identity import FileChangeAttribution, FileChangeKind, FileVersion
from mote.contracts.tool.identity import ToolInvocationIdentity


@dataclass(frozen=True, slots=True)
class HookIdentity:
    session_id: str = ""
    cwd: str = ""
    transcript_path: str = ""


@dataclass(frozen=True, slots=True)
class PreToolUsePayload:
    identity: ToolInvocationIdentity
    tool_name: str = ""
    tool_input: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        frozen = freeze_json(self.tool_input, path="pre-tool hook input")
        if not isinstance(frozen, Mapping):
            raise TypeError("pre-tool hook input must be an object")
        object.__setattr__(self, "tool_input", cast(Mapping[str, JsonValue], frozen))


@dataclass(frozen=True, slots=True)
class PostToolUsePayload:
    identity: ToolInvocationIdentity
    tool_name: str = ""
    tool_input: Mapping[str, JsonValue] = field(default_factory=dict)
    tool_response: str = ""
    success: bool = False
    error: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        tool_input = freeze_json(self.tool_input, path="post-tool hook input")
        if not isinstance(tool_input, Mapping):
            raise TypeError("post-tool hook input must be an object")
        object.__setattr__(self, "tool_input", cast(Mapping[str, JsonValue], tool_input))
        if self.error is not None:
            error = freeze_json(self.error, path="post-tool hook error")
            if not isinstance(error, Mapping):
                raise TypeError("post-tool hook error must be an object")
            object.__setattr__(self, "error", cast(Mapping[str, JsonValue], error))


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
    path: str
    change_type: FileChangeKind
    prior_version: FileVersion
    version: FileVersion
    attribution: FileChangeAttribution


@dataclass(frozen=True, slots=True)
class PreToolUseInvocation:
    identity: HookIdentity
    permission_mode: PermissionMode | None
    payload: PreToolUsePayload
    schema_version: Literal[1] = 1
    kind: Literal["pre_tool_use"] = "pre_tool_use"


@dataclass(frozen=True, slots=True)
class PostToolUseInvocation:
    identity: HookIdentity
    payload: PostToolUsePayload
    schema_version: Literal[1] = 1
    kind: Literal["post_tool_use"] = "post_tool_use"


@dataclass(frozen=True, slots=True)
class UserPromptSubmitInvocation:
    identity: HookIdentity
    payload: UserPromptSubmitPayload
    schema_version: Literal[1] = 1
    kind: Literal["user_prompt_submit"] = "user_prompt_submit"


@dataclass(frozen=True, slots=True)
class SessionStartInvocation:
    identity: HookIdentity
    payload: SessionStartPayload
    schema_version: Literal[1] = 1
    kind: Literal["session_start"] = "session_start"


@dataclass(frozen=True, slots=True)
class StopInvocation:
    identity: HookIdentity
    payload: StopPayload
    schema_version: Literal[1] = 1
    kind: Literal["stop"] = "stop"


@dataclass(frozen=True, slots=True)
class PreCompactInvocation:
    identity: HookIdentity
    payload: CompactPayload
    schema_version: Literal[1] = 1
    kind: Literal["pre_compact"] = "pre_compact"


@dataclass(frozen=True, slots=True)
class PostCompactInvocation:
    identity: HookIdentity
    payload: CompactPayload
    schema_version: Literal[1] = 1
    kind: Literal["post_compact"] = "post_compact"


@dataclass(frozen=True, slots=True)
class FileChangedInvocation:
    identity: HookIdentity
    payload: FileChangedPayload
    schema_version: Literal[1] = 1
    kind: Literal["file_changed"] = "file_changed"


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


__all__ = [
    "CompactPayload",
    "FileChangedInvocation",
    "FileChangedPayload",
    "HookIdentity",
    "HookInvocation",
    "PostCompactInvocation",
    "PostToolUseInvocation",
    "PostToolUsePayload",
    "PreCompactInvocation",
    "PreToolUseInvocation",
    "PreToolUsePayload",
    "SessionStartInvocation",
    "SessionStartPayload",
    "StopInvocation",
    "StopPayload",
    "UserPromptSubmitInvocation",
    "UserPromptSubmitPayload",
]
