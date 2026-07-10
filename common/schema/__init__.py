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
    "ToolMessage": ("messages", "ToolMessage"),
    "ResourceMessage": ("messages", "ResourceMessage"),
    "LLMCallContext": ("messages", "LLMCallContext"),
    # serialization
    "BaseSerialization": ("serialization", "BaseSerialization"),
    # env
    "BaseEnvironment": ("env", "BaseEnvironment"),
    # gym_env (RL/gym interface, decoupled from the orchestration core)
    "BaseEnvActionType": ("gym_env", "BaseEnvActionType"),
    "BaseEnvAction": ("gym_env", "BaseEnvAction"),
    "BaseEnvObsType": ("gym_env", "BaseEnvObsType"),
    "BaseEnvObsParams": ("gym_env", "BaseEnvObsParams"),
    "GymEnvironment": ("gym_env", "GymEnvironment"),
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
    # askuser
    "ASK_USER_QUESTION_CHIP_WIDTH": ("askuser", "ASK_USER_QUESTION_CHIP_WIDTH"),
    "AskUserQuestionOption": ("askuser", "AskUserQuestionOption"),
    "AskUserQuestionItem": ("askuser", "AskUserQuestionItem"),
    "AskUserQuestionInput": ("askuser", "AskUserQuestionInput"),
    "AskUserQuestionAnswer": ("askuser", "AskUserQuestionAnswer"),
    "AskUserQuestionAnswers": ("askuser", "AskUserQuestionAnswers"),
    # context
    "ContextManagerConfig": ("context", "ContextManagerConfig"),
    "TokenState": ("context", "TokenState"),
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
    # sandbox_runtime_config
    "SandboxRuntimeConfig": ("sandbox_runtime_config", "SandboxRuntimeConfig"),
    # permission_types
    "PermissionMode": ("permission_types", "PermissionMode"),
    "PermissionBehavior": ("permission_types", "PermissionBehavior"),
    "GrantScope": ("permission_types", "GrantScope"),
    "RuleSource": ("permission_types", "RuleSource"),
    "RiskLevel": ("permission_types", "RiskLevel"),
    "DecisionReason": ("permission_types", "DecisionReason"),
    "PermissionDecision": ("permission_types", "PermissionDecision"),
    "PermissionFacts": ("permission_types", "PermissionFacts"),
    "PermissionRule": ("permission_types", "PermissionRule"),
    # hook_config
    "HookConfig": ("hook_config", "HookConfig"),
    "HookCommandHandler": ("hook_config", "HookCommandHandler"),
    "HookMatcherGroup": ("hook_config", "HookMatcherGroup"),
    # lsp_config
    "LspConfig": ("lsp_config", "LspConfig"),
    "LspServerConfig": ("lsp_config", "LspServerConfig"),
    # file_watch_config
    "FileWatchConfig": ("file_watch_config", "FileWatchConfig"),
    # node_status
    "BgStatus": ("node_status", "BgStatus"),
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
