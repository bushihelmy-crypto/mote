#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RoleSchema — static configuration, determined at deploy time.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from mote.common.prompt.role import CMD_PROMPT, SYSTEM_PROMPT
from mote.common.schema import FileWatchConfig, HookConfig, LspConfig, PermissionConfig


class RoleSchema(BaseModel):
    """Static configuration for a Role. Determined at deploy time, does not change at runtime."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # --- Identity ---
    # ``name``/``profile`` drive message routing + signing (display_name) on the
    # Role/bus. Neither reaches the rendered system prompt.
    name: str = "Zero"
    profile: str = "Role"

    # --- Prompt templates ---
    system_prompt: str = SYSTEM_PROMPT
    cmd_prompt: str = CMD_PROMPT

    # --- Command protocol ---
    # How the Role exchanges commands with the LLM:
    #   "xml"    -> text protocol (XML command blocks + OUTPUT_SECTION)
    #   "native" -> provider-native tool-use (JSON-Schema tool specs + tool_calls)
    # Defaults to "native"; "xml" is the model-agnostic fallback.
    command_protocol: Literal["xml", "native"] = "native"
    # Note: the native tool-spec envelope (OpenAI vs Anthropic) is NOT configured
    # here — it is inferred from the LLM config at runtime, since it must match
    # the client that actually issues the request. See infer_native_tool_provider.

    # --- Loop control ---
    max_react_loop: int = 50
    max_consecutive_react_limit: int = 10
    # Hard per-agent budget cap in USD, keyed off this agent's own accrued spend
    # (``context.cost_manager.total_cost``). A sibling run-limit to max_react_loop:
    # at 80% the loop surfaces a soft CLI notice (once), at 100% it stops before
    # the next think — no further LLM access. ``0.0`` disables the gate entirely
    # (opt-in; the default), so an unbudgeted agent triggers neither threshold.
    max_cost: float = 0.0
    # Which strategies the graph's per-turn factories build. Each is a key into a
    # builder registry in RoleComponents: ``loop_kind`` selects the react-loop
    # class ("react" -> the standard think→act ReActLoop) and ``think_kind`` the
    # think engine ("default" -> ThinkEngine). Both are read by the factory at
    # call-time (not at build-time), so a future tool can swap the strategy
    # mid-session and the next turn's fresh instance honours the new choice.
    loop_kind: str = "react"
    think_kind: str = "default"
    # Auto-continue budget: max times a TurnEnd control subscriber may block the
    # "stop" to force another turn (Stop-hook semantics). 0 disables the seam
    # entirely — the run loop executes exactly once per call (the default).
    max_auto_continue: int = 0

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
        "GetNodeState",
        "CodeReview",
        "WebBrowser",
        "Skill",
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
    # --- Hooks ---
    # Opt-in agent-lifecycle hooks (command handlers). When None (default) and
    # no callbacks are registered programmatically, no hook layer is engaged
    # (the default). Python callbacks are registered on the HookManager, not
    # declared here.
    hooks: Optional[HookConfig] = None

    # --- LSP ---
    # Opt-in language-server diagnostics. When None (default), no LSP layer is
    # engaged. Set an LspConfig (with servers) to launch
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
    # When True (default), file-mutating tools (Write/Edit) record a
    # before-image of each file just before overwriting it, into the session's
    # blob store + rollout log (the truth source for diff/undo/rollback). Set
    # False to disable snapshotting (saves disk; loses undo history).
    record_file_history: bool = True
    # Storage backend for the before-images. "auto" (default) picks "git" (an
    # independent bare git object db, cheaper on disk) when the working dir is
    # inside a code repo and the git binary is present, else the plain "blob"
    # store. Force "blob" or "git" to override the heuristic.
    snapshot_backend: Literal["auto", "blob", "git"] = "auto"
    # When True (default), the persistent terminal records its final environment
    # state (cwd + env diff vs the shell's launch baseline) into the rollout, so
    # a resumed session can re-seed a fresh shell to that state without re-running
    # any user commands. Set False to disable (the shell starts clean on resume).
    record_terminal_state: bool = True
    # When True (default), the persistent Python kernel records its final
    # environment state (cwd + env diff vs the kernel's launch baseline) into the
    # rollout, so a resumed session can re-seed a fresh kernel to that state
    # without re-running any user code. Only cwd + env vars are restored — NOT
    # Python variables/imports/functions (the model re-establishes those from the
    # replayed message history; no code is auto-rerun, avoiding side effects).
    # Set False to disable (the kernel starts clean on resume).
    record_kernel_state: bool = True
    # When True (default), the persistent browser records its final browsing
    # state (open-tab URLs + active tab + an optional storage_state carrying the
    # logged-in session: cookies / localStorage) into the rollout, so a resumed
    # session can re-open the same tabs seeded with that session without
    # re-running any navigation/click actions. Set False to disable — relevant
    # for privacy, since storage_state may carry sensitive cookies (the browser
    # then starts clean on resume).
    record_browser_state: bool = True
    # When True, the persistent browser launches with a visible window (headed);
    # default False runs headless. A headed window is useful for watching the
    # agent browse or for sites that behave differently without a real display.
    browser_headless: bool = True
    # When True, the persistent browser applies lightweight, opt-in anti-bot-
    # detection on launch: a realistic desktop Chrome user-agent (replacing the
    # "HeadlessChrome" default), en-US locale + Accept-Language, a fixed
    # viewport/timezone, the ``--disable-blink-features=AutomationControlled``
    # launch flag, and an init script hiding the ``navigator.webdriver`` signal.
    # Default True (baseline hygiene: the headless default otherwise leaks a
    # "HeadlessChrome" UA + ``navigator.webdriver=true``). Defeats only passive
    # checks, not active challenges (CAPTCHA / Cloudflare) — set False to opt out.
    browser_stealth: bool = True
    # Which locale bundle the stealth fingerprint uses (only consulted when
    # ``browser_stealth`` is on). Each bundle keeps locale + timezone +
    # Accept-Language + navigator.languages mutually consistent; the region
    # should match the exit IP (a zh-CN locale on a US IP is itself a bot tell).
    #   "auto" (default) — infer zh vs en from the host's locale env vars, which
    #     (absent a proxy) is the effective exit region.
    #   "en" — en-US / America/New_York.  "zh" — zh-CN / Asia/Shanghai.
    browser_locale: Literal["auto", "en", "zh"] = "auto"
    # Optional upstream proxy for the persistent browser — a single URL string
    # giving the whole session one exit IP: ``http://host:port``,
    # ``http://user:pass@host:port``, or ``socks5://host:port`` (a bare
    # ``host:port`` is treated as http). Empty (default) means a direct
    # connection. Changing the exit IP is the highest-value anti-blocking lever
    # (rate limits / CAPTCHAs key off IP reputation); keep the proxy's region
    # consistent with ``browser_locale`` / timezone so the fingerprint agrees.
    browser_proxy: str = ""

    # --- Memory config ---
    enable_memory: bool = True
    enable_router: bool = False

    # --- Behavior flags ---
    observe_all_msg_from_buffer: bool = True

    # --- Derived properties ---
    @property
    def display_name(self) -> str:
        """Human-readable name, e.g. 'Zero(Role)'."""
        return f"{self.name}({self.profile})" if self.profile else self.name
