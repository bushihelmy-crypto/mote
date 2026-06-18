#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RoleSchema — static configuration, determined at deploy time.
"""

from __future__ import annotations

from typing import ClassVar, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from metagpt.common.schema import FileWatchConfig, HookConfig, LspConfig, PermissionConfig
from metagpt.common.prompt.role import (
    CMD_PROMPT,
    ROLE_INSTRUCTION,
    SUMMARY_PROMPT,
    SUMMARY_WITH_RECOMMEND_PROMPT,
    SYSTEM_PROMPT,
)


class RoleSchema(BaseModel):
    """Static configuration for a Role. Determined at deploy time, does not change at runtime."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # --- Identity ---
    name: str = "Zero"
    profile: str = "Role"
    goal: str = ""
    constraints: str = ""
    desc: str = ""
    role_id: str = ""

    # --- Prompt templates ---
    system_prompt: str = SYSTEM_PROMPT
    cmd_prompt: str = CMD_PROMPT
    instruction: str = ROLE_INSTRUCTION
    example: str = ""
    # End-of-session summary prompts (consumed by Role.end_session). Kept on the
    # schema like the other deploy-time templates so a Role can override the
    # summary voice without patching role_zero imports. The "with recommend"
    # variant is selected when need_end_recommendations_tag is set.
    summary_prompt: str = SUMMARY_PROMPT
    summary_with_recommend_prompt: str = SUMMARY_WITH_RECOMMEND_PROMPT

    # --- Command protocol ---
    # How the Role exchanges commands with the LLM:
    #   "xml"    -> legacy text protocol (XML command blocks + OUTPUT_SECTION)
    #   "native" -> provider-native tool-use (JSON-Schema tool specs + tool_calls)
    # Defaults to "xml" for backward compatibility (model-agnostic).
    command_protocol: Literal["xml", "native"] = "native"
    # Note: the native tool-spec envelope (OpenAI vs Anthropic) is NOT configured
    # here — it is inferred from the LLM config at runtime, since it must match
    # the client that actually issues the request. See infer_native_tool_provider.

    # --- Loop control ---
    max_react_loop: int = 50
    max_consecutive_react_limit: int = 10

    # --- Tools / Agent declarations ---
    tools: list[str] = [
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "Bash",
        "Terminal",
        "Jupyter",
        "Agent",
        "AskUserQuestion",
        "Sleep",
        "ResumeTasks",
        "CancelTasks",
        "GetNodeState",
        "MediaPipeline"
    ]
    mcps: list[str] = []
    agents: list[str] = []
    skills: list[str] = []

    # --- Permissions ---
    # Tool-approval policy. The default engages the PermissionEngine in
    # ``default`` mode, so every tool call with no matching allow rule prompts
    # the user for confirmation. Set ``mode="bypass"`` (or specific allow rules)
    # to loosen this, or build a custom PermissionConfig for finer control.
    permissions: Optional[PermissionConfig] = Field(default_factory=PermissionConfig)
    language : str = "中文"
    # --- Hooks ---
    # Opt-in agent-lifecycle hooks (command handlers). When None (default) and
    # no callbacks are registered programmatically, no hook layer is engaged
    # (legacy behavior). Python callbacks are registered on the HookManager, not
    # declared here.
    hooks: Optional[HookConfig] = None

    # --- LSP ---
    # Opt-in language-server diagnostics. When None (default), no LSP layer is
    # engaged (legacy behavior). Set an LspConfig (with servers) to launch
    # language servers lazily on relevant file edits and surface diagnostics
    # back into context at the next turn boundary.
    lsp: Optional[LspConfig] = None

    # --- File watching ---
    # Opt-in external-file-change watcher. When None (default), no watcher runs.
    # Set a FileWatchConfig (enabled=True) to poll the project root and fire
    # FileChanged hooks on external changes; the agent's own tool-driven writes
    # are suppressed (via FileMutatedEvent on the event bus) so they don't echo
    # back. Requires a hook layer to consume the FileChanged events.
    file_watch: Optional[FileWatchConfig] = None

    # --- File history ---
    # When True (default), file-mutating tools (Write/Edit/NotebookEdit) record a
    # before-image of each file just before overwriting it, into the session's
    # blob store + rollout log (the truth source for diff/undo/rollback). Set
    # False to disable snapshotting (saves disk; loses undo history).
    record_file_history: bool = True
    # Storage backend for the before-images. "auto" (default) picks "git" (an
    # independent bare git object db, cheaper on disk) when the working dir is
    # inside a code repo and the git binary is present, else the plain "blob"
    # store. Force "blob" or "git" to override the heuristic.
    snapshot_backend: Literal["auto", "blob", "git"] = "auto"

    # --- Memory / summary config ---
    enable_memory: bool = True
    memory_k: int = 30
    use_summary: bool = True
    enable_router: bool = False

    # --- Behavior flags ---
    delegated_from: str = ""
    observe_all_msg_from_buffer: bool = True
    need_end_recommendations_tag: ClassVar[bool] = False

    # --- Derived properties ---
    @property
    def display_name(self) -> str:
        """Human-readable name, e.g. 'Zero(Role)'."""
        return f"{self.name}({self.profile})" if self.profile else self.name
