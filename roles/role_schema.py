#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RoleSchema — static configuration, determined at deploy time.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from mote.common.prompt.role import CMD_PROMPT, ROLE_INFO, SYSTEM_PROMPT
from mote.common.schema import FileWatchConfig, HookConfig, LspConfig, PermissionConfig


class BrowserClientCert(BaseModel):
    """One client TLS certificate for mutual-TLS (mTLS) login on an origin.

    Mirrors Playwright's ``new_context(client_certificates=[...])`` entry: an
    ``origin`` the cert is presented to plus EITHER a PEM ``cert_path`` +
    ``key_path`` pair OR a PKCS#12 ``pfx_path``. ``passphrase`` may be a secret
    placeholder (``<agent-vault:KEY>`` / ``<secret:dotted.path>``) — the tool
    expands it from the vault at launch, so the plaintext never rides config or
    history. All paths are host filesystem paths readable by the process.
    """

    model_config = ConfigDict(extra="forbid")

    # The exact origin the cert authenticates to, e.g. "https://portal.example.com".
    origin: str
    # PEM cert + private key (use these OR pfx_path, not both).
    cert_path: str = ""
    key_path: str = ""
    # PKCS#12 bundle (alternative to the PEM pair).
    pfx_path: str = ""
    # Optional key/PKCS#12 passphrase. May be a secret placeholder.
    passphrase: str = ""


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
    # The role's own charter — its task DOMAIN + domain conventions — rendered
    # LAST in the system prompt's dynamic region (${role_info}). Extracted out of
    # ``system_prompt`` so the shared prefix carries only principles every agent
    # holds; override this alone to retask the engine to another domain without
    # touching (or busting the cache of) the shared prefix. "" emits nothing.
    role_info: str = ROLE_INFO

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
    # Hard per-agent budget cap in USD, keyed off this agent's own accrued spend
    # (``context.cost_manager.total_cost``). The run's single spend ceiling: at
    # 80% the loop surfaces a soft CLI notice (once), at 100% it stops before the
    # next think — no further LLM access. ``0.0`` disables the gate entirely
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
    deferred_tools: list[str] = [
        "Terminal",
        "Jupyter",
        "Agent",
        "RunGraph",
        "Sleep",
        "WebBrowser",
        "WebSearch",
        "GenerateMedia",
        "Skill",
        "DeviceUse",
    ]
    tools: list[str] = [
        "Read",
        "Edit",
        "Search",
        "Bash",
        "AskUserQuestion",
        "SearchTools",
        # Deferred tools MUST also live here: ``deferred_tools`` is a
        # visibility-only subset of ``tools`` (a deferred tool is still bound and
        # dispatchable, its schema is merely withheld until searched). Omitting
        # them here would leave them UNBOUND — invisible to SearchTools, so the
        # model could never discover, reveal, or call them.
        "GenerateMedia",
        "Terminal",
        "Jupyter",
        "Agent",
        "RunGraph",
        "Sleep",
        "WebBrowser",
        "WebSearch",
        "Skill",
        "DeviceUse",
    ]
    mcps: list[str] = []
    agents: list[str] = []
    skills: list[str] = []
    # Tool-search deferral: a subset of ``tools`` whose full schema is withheld
    # from the model until it discovers the tool via the ``SearchTools`` meta-tool
    # (which is auto-bound whenever this list is non-empty). Deferral is
    # visibility-only — a deferred tool stays fully dispatchable once revealed.
    # Declaring deferral per-role (not on the tool class) lets the same tool be
    # core for one role and deferred for another; keeps the steady per-turn token
    # cost flat as the toolset grows. Empty (default) → every tool is always
    # visible and no search tool is bound (zero overhead when unused).

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
    # When True (default), every file-mutating tool result is split into change
    # *hunks* attributed to the agent (which turn / tool produced each) and
    # appended to the session's durable hunk ledger — the truth source for
    # per-hunk review (accept/reject/undo) and change attribution. Complements
    # record_file_history (whole-file before-images): this is line-level
    # attribution. Set False to disable (no hunk ledger; loses per-hunk review).
    record_hunks: bool = True
    # When True (default), a whole-working-tree checkpoint is captured at each
    # user-turn boundary into the session's dedicated git object db, so the user
    # can roll the entire tree back to any prior turn (the /rewind command). This
    # engages ONLY inside a git-backed code workspace (mirroring snapshot_backend:
    # a non-repo cwd makes the feature inert). Complements record_file_history —
    # per-file before-images are fine-grained, this is a whole-tree undo. Set
    # False to disable (no per-turn tree snapshots; loses /rewind).
    record_checkpoints: bool = True
    # When True (default), a one-line session title is generated from the first
    # user prompt via a single cheap auxiliary-model call and appended to the
    # rollout (MetaUpdateEvent.title), so the session listing shows a meaningful
    # label instead of a bare id. Fire-and-forget + once-per-session (resume-safe:
    # an already-titled session never re-generates). Set False to disable (the
    # listing then falls back to the session id / first-prompt preview).
    generate_title: bool = True
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
    # Durable browser-login profile name. When set, the persistent browser seeds
    # its session from — and persists its login back into — an ENCRYPTED profile
    # under ``~/.mote/browser_profiles/`` (reusing the vault key), so a logged-in
    # session survives across runs with no re-login (and the session cookies stay
    # OUT of the plaintext rollout). Empty (default) keeps the current ephemeral
    # behavior: login lasts only for the session and, if ``record_browser_state``
    # is on, is captured into the rollout for same-session resume only.
    browser_profile: str = ""
    # Client TLS certificates for mutual-TLS (mTLS) logins — sites that require
    # the client to present a certificate (common in enterprise / government
    # portals). Each entry pins a cert to an origin (see ``BrowserClientCert``);
    # applied to the browser context at launch. Empty (default) = no mTLS.
    browser_client_certs: list[BrowserClientCert] = Field(default_factory=list)
    # Chrome DevTools Protocol endpoint (e.g. "http://127.0.0.1:9222"). When set,
    # the persistent browser ATTACHES to an already-running Chrome/Chromium over
    # CDP instead of launching its own — so the agent drives the human's real
    # browser with its existing logins, passkeys, and extensions (the ultimate
    # login-reuse rung). While attached, stealth / proxy / storage_state seeding
    # and durable profiles are ignored (the real browser owns that config) and
    # teardown only DISCONNECTS — it never closes the human's browser. Empty
    # (default) = launch a private browser as before.
    browser_cdp_endpoint: str = ""

    # --- Memory config ---
    enable_memory: bool = True

    # --- Behavior flags ---
    observe_all_msg_from_buffer: bool = True

    # --- Derived properties ---
    @property
    def display_name(self) -> str:
        """Human-readable name, e.g. 'Zero(Role)'."""
        return f"{self.name}({self.profile})" if self.profile else self.name
