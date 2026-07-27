#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RoleSchema — static configuration, determined at deploy time.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from mote.contracts.settings.hooks import HookConfig
from mote.contracts.settings.lsp import LspConfig
from mote.contracts.settings.permissions import PermissionConfig
from mote.contracts.settings.watching import FileWatchConfig
from mote.kernel.agent_spec import AgentSpec


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


class RoleSchema(AgentSpec):
    """Agent definition plus Runtime deployment and reliability policy."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

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
    # Set a FileWatchConfig (enabled=True) to poll exact File Operations versions
    # and emit typed FileChanged events. Hooks and per-turn freshness reminders
    # consume that shared event; only exact durable File Operations commits are
    # attributed as managed writes.
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
    # Optional Chrome DevTools Protocol endpoint for a browser the user already
    # launched and logged into. When set, WebBrowser attaches to that browser
    # instead of starting its own headless Chromium. Keep it loopback-only: a
    # CDP endpoint grants full control of the browser profile.
    browser_cdp_endpoint: str = ""
    # Durable browser-login profile name. When set, the persistent browser seeds
    # its session from — and persists its login back into — an ENCRYPTED profile
    # under ``~/.mote/browser_profiles/`` (reusing the vault key), so a logged-in
    # session survives across runs with no re-login (and the session cookies stay
    # OUT of the plaintext rollout). Empty (default) keeps the current ephemeral
    # behavior: login lasts only for the session and is captured into the rollout
    # for same-session resume only.
    browser_profile: str = ""
    # Client TLS certificates for mutual-TLS (mTLS) logins — sites that require
    # the client to present a certificate (common in enterprise / government
    # portals). Each entry pins a cert to an origin (see ``BrowserClientCert``);
    # applied to the browser context at launch. Empty (default) = no mTLS.
    browser_client_certs: list[BrowserClientCert] = Field(default_factory=list)
