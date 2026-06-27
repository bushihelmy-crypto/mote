"""Permission config — deploy-time, pure-data settings.

Lives in ``common/schema`` alongside ``tool_config.py`` so both ``RoleSchema``
(which declares it) and ``ToolExecutor`` (which enforces it) can reference it
without importing the executor package. The enforcement logic stays in
``metagpt.executor.permission``.

Backward compatibility: a Role with ``permissions=None`` (the default) keeps the
old behavior — tools run with no approval layer. The engine is only engaged when
a Role explicitly opts in by setting a ``PermissionConfig``.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# Single source of truth for the approval-mode Literal (pure-data, no executor dep).
from metagpt.common.schema.permission_types import PermissionMode
from metagpt.common.schema.sandbox_runtime_config import SandboxRuntimeConfig

# Sandbox axis — ORTHOGONAL to the approval mode above. The mode decides whether
# to ask the user; the sandbox decides the filesystem/network boundary a tool
# executes within. (Codex's sandbox model.)
#   read-only       -> no filesystem writes at all
#   workspace-write -> writes confined to the cwd + writable_roots
#   full            -> no boundary (enforcement disabled)
SandboxMode = Literal["read-only", "workspace-write", "full"]
# Network policy is carried for completeness but NOT enforced in phase 2 (true
# network isolation needs OS-level sandboxing); treated as advisory metadata.
NetworkPolicy = Literal["restricted", "enabled"]


class SandboxConfig(BaseModel):
    """Filesystem/network execution boundary, nested under PermissionConfig.

    A logical (path-checking) sandbox, not an OS-level one: file-mutating tools
    are checked against ``mode`` + writable roots before they run, and a
    violation is escalated to the user for a one-off / session exception rather
    than hard-failed (Codex's ``RequireEscalated`` flow).
    """

    mode: SandboxMode = Field(
        default="workspace-write",
        description="Filesystem boundary: read-only | workspace-write | full.",
    )
    writable_roots: list[str] = Field(
        default_factory=list,
        description="Extra absolute (or cwd-relative) roots writable beyond the cwd.",
    )
    network: NetworkPolicy = Field(
        default="restricted",
        description="Advisory network policy (not enforced in phase 2).",
    )
    allowed_domains: list[str] = Field(
        default_factory=list,
        description=(
            "Domain allowlist forwarded to the OS-level runtime's network proxy "
            "(glob: '*.x' / '**.x' / exact). Only meaningful when an OS-level "
            "SandboxRuntimeConfig is enabled; ignored by the logical guard."
        ),
    )


class PermissionConfig(BaseModel):
    """Per-Role permission policy, declared on :class:`RoleSchema`.

    Rules are written in the familiar ``Tool(pattern)`` form, e.g.::

        allow = ["Read", "Grep", "Glob", "Bash(git*)"]
        deny  = ["Bash(rm -rf*)"]
        ask   = ["Bash(npm publish*)", "Write"]

    Matching semantics (see ``executor/permission/rule_matcher.py``):
      * a bare ``Tool`` matches every call to that tool;
      * ``Tool(pattern)`` matches when the tool's permission-target string
        matches ``pattern`` via ``fnmatch`` (so ``*`` / ``?`` globbing works);
      * ``mcp__server`` matches every tool under that MCP server.

    Precedence: a matching ``deny`` always wins, then ``ask``, then ``allow``;
    the ``mode`` decides the fallback for anything no rule matched.
    """

    mode: PermissionMode = Field(
        default="default",
        description="Coarse approval stance: default | acceptEdits | plan | bypass | dontAsk.",
    )
    allow: list[str] = Field(default_factory=list, description="Rules auto-approved without prompting.")
    deny: list[str] = Field(default_factory=list, description="Rules always blocked (bypass-immune).")
    ask: list[str] = Field(default_factory=list, description="Rules that always prompt the user (bypass-immune).")
    sandbox: Optional[SandboxConfig] = Field(
        default=None,
        description="Optional filesystem sandbox. None disables boundary checks (full access).",
    )
    runtime: Optional[SandboxRuntimeConfig] = Field(
        default=None,
        description=(
            "Optional OS-level sandbox runtime (bwrap + hardening + network proxy). "
            "None disables OS-level isolation, leaving only the logical boundary."
        ),
    )
