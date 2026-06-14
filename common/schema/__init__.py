"""metagpt.common.schema — consolidated schema package.

Re-exports all public names so ``from metagpt.common.schema import Message`` works.

Uses lazy ``__getattr__`` to avoid eagerly importing all submodules at package
init time, which prevents circular imports when submodule transitive deps import
back from this package.
"""

from __future__ import annotations

import importlib
from typing import Any

# Mapping: public name -> (submodule relative name, attribute name)
_LAZY_MAPPING: dict[str, tuple[str, str]] = {
    # messages
    "Message": ("messages", "Message"),
    "UserMessage": ("messages", "UserMessage"),
    "SystemMessage": ("messages", "SystemMessage"),
    "AIMessage": ("messages", "AIMessage"),
    "LLMCallContext": ("messages", "LLMCallContext"),
    # serialization
    "BaseSerialization": ("serialization", "BaseSerialization"),
    # env
    "BaseEnvActionType": ("env", "BaseEnvActionType"),
    "BaseEnvAction": ("env", "BaseEnvAction"),
    "BaseEnvObsType": ("env", "BaseEnvObsType"),
    "BaseEnvObsParams": ("env", "BaseEnvObsParams"),
    "BaseEnvironment": ("env", "BaseEnvironment"),
    # document
    "CauseBy": ("document", "CauseBy"),
    "ActionOutput": ("document", "ActionOutput"),
    "SerializationMixin": ("document", "SerializationMixin"),
    "Document": ("document", "Document"),
    "Documents": ("document", "Documents"),
    "Resource": ("document", "Resource"),
    # queue
    "MessagePriority": ("queue", "MessagePriority"),
    "QueuedMessage": ("queue", "QueuedMessage"),
    "MessageQueue": ("queue", "MessageQueue"),
    "LongTermMemoryItem": ("queue", "LongTermMemoryItem"),
    # tasks
    "BgStatus": ("tasks", "BgStatus"),
    "TaskType": ("tasks", "TaskType"),
    "BackgroundTaskNotification": ("tasks", "BackgroundTaskNotification"),
    "is_bg_notification": ("tasks", "is_bg_notification"),
    "BgTaskResult": ("tasks", "BgTaskResult"),
    "TaskMeta": ("tasks", "TaskMeta"),
    # askuser
    "ASK_USER_QUESTION_CHIP_WIDTH": ("askuser", "ASK_USER_QUESTION_CHIP_WIDTH"),
    "AskUserQuestionOption": ("askuser", "AskUserQuestionOption"),
    "AskUserQuestionItem": ("askuser", "AskUserQuestionItem"),
    "AskUserQuestionInput": ("askuser", "AskUserQuestionInput"),
    # context
    "ContextManagerConfig": ("context", "ContextManagerConfig"),
    "TokenState": ("context", "TokenState"),
    "MicrocompactResult": ("context", "MicrocompactResult"),
    "AutocompactResult": ("context", "AutocompactResult"),
    # tool_config
    "DEFAULT_MAX_RESULT_SIZE_CHARS": ("tool_config", "DEFAULT_MAX_RESULT_SIZE_CHARS"),
    "BYTES_PER_TOKEN": ("tool_config", "BYTES_PER_TOKEN"),
    "PREVIEW_SIZE_BYTES": ("tool_config", "PREVIEW_SIZE_BYTES"),
    "PERSISTED_OUTPUT_OPEN_TAG": ("tool_config", "PERSISTED_OUTPUT_OPEN_TAG"),
    "PERSISTED_OUTPUT_CLOSE_TAG": ("tool_config", "PERSISTED_OUTPUT_CLOSE_TAG"),
    "TOOL_MAX_RESULT_SIZE_CHARS": ("tool_config", "TOOL_MAX_RESULT_SIZE_CHARS"),
    "TOOL_RESULTS_SUBDIR": ("tool_config", "TOOL_RESULTS_SUBDIR"),
    "ToolResultLimitConfig": ("tool_config", "ToolResultLimitConfig"),
    # permission_config
    "PermissionConfig": ("permission_config", "PermissionConfig"),
    "SandboxConfig": ("permission_config", "SandboxConfig"),
    # think
    "ThinkResult": ("think", "ThinkResult"),
}

__all__ = list(_LAZY_MAPPING)


def __getattr__(name: str) -> Any:
    # Schema submodule lookup
    if name in _LAZY_MAPPING:
        submodule, attr = _LAZY_MAPPING[name]
        mod = importlib.import_module(f".{submodule}", __name__)
        val = getattr(mod, attr)
        globals()[name] = val  # cache so subsequent access is O(1)
        return val

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
