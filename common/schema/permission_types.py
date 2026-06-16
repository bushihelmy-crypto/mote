"""Permission types — pure data, no logic.

Lives in ``common/schema`` (alongside ``tool_config.py``) and is kept
dependency-free (only stdlib typing/dataclasses) so any layer — tools, the
permission engine, the config schema, the hook layer — can import these as the
single source of truth without circular imports. This mirrors how Claude Code
isolates ``src/types/permissions.ts`` from the permission engine; the runtime
enforcement logic stays in ``metagpt.executor.permission``.

Two orthogonal concepts live here:
  * **Mode** — the coarse stance toward asking the user (Claude Code semantics).
  * **Rule / Behavior** — fine-grained allow/deny/ask matching of a tool call.

The runtime decision pipeline (see ``executor/permission/engine.py``) combines
them into a single ``PermissionDecision``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

# ---------------------------------------------------------------------------
# Enumerations (kept as Literals — no runtime enum machinery needed)
# ---------------------------------------------------------------------------

# Coarse stance, borrowed from Claude Code's permission modes:
#   default      -> ask the user for anything not pre-authorized
#   acceptEdits  -> auto-allow filesystem-mutating tools (Edit/Write/...)
#   plan         -> read-only preview: deny anything that would act
#   bypass       -> allow everything EXCEPT bypass-immune deny/ask rules
#   dontAsk      -> never prompt; anything that would ask is denied (fail-closed)
PermissionMode = Literal["default", "acceptEdits", "plan", "bypass", "dontAsk"]

# The three terminal behaviors of a rule / decision.
PermissionBehavior = Literal["allow", "deny", "ask"]

# How long a user-granted approval lasts (fusion of CC + Codex):
#   once    -> this call only (no rule stored)
#   session -> remembered for the rest of this session (in-memory rule)
#   persist -> written back to durable config (phase 2; treated as session now)
GrantScope = Literal["once", "session", "persist"]

# Where a rule came from. Lower in the list does NOT mean higher priority — a
# matching ``deny`` always wins regardless of source (see engine.py).
RuleSource = Literal["role", "project", "local", "session"]

# Coarse risk label a tool may self-declare (Codex Guardian-style). Advisory in
# phase 1; consumed by the classifier in phase 3.
RiskLevel = Literal["low", "medium", "high"]


# ---------------------------------------------------------------------------
# Decision plumbing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionReason:
    """Why a decision was reached — surfaced in logs and user-facing messages.

    ``type`` is one of: ``rule`` | ``mode`` | ``tool_check`` | ``safety`` |
    ``default`` | ``user``.
    """

    type: str
    detail: str = ""


@dataclass
class PermissionDecision:
    """The outcome of evaluating a single tool call.

    After the engine finishes, ``behavior`` is only ever ``allow`` or ``deny``
    (any ``ask`` is resolved internally by prompting the user). Tools that
    implement ``check_permissions`` may return a decision with ``ask`` to defer
    to the user.

    ``updated_args`` lets an approver narrow/rewrite the tool arguments before
    execution (e.g. confine a path); ``None`` means "run with original args".
    """

    behavior: PermissionBehavior
    reason: DecisionReason
    message: str = ""
    updated_args: Optional[dict] = None

    # --- ergonomic constructors ---
    @classmethod
    def allow(cls, reason_type: str, detail: str = "", *, updated_args: Optional[dict] = None) -> "PermissionDecision":
        return cls("allow", DecisionReason(reason_type, detail), updated_args=updated_args)

    @classmethod
    def deny(cls, reason_type: str, detail: str = "", *, message: str = "") -> "PermissionDecision":
        return cls("deny", DecisionReason(reason_type, detail), message=message or detail)

    @classmethod
    def ask(cls, reason_type: str, detail: str = "", *, message: str = "") -> "PermissionDecision":
        return cls("ask", DecisionReason(reason_type, detail), message=message or detail)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionRule:
    """A single allow/deny/ask rule.

    ``tool_name`` matches a tool by primary name, or an MCP namespace
    (``mcp__server`` matches every ``mcp__server__tool``), or a glob
    (``mcp__server__*``).

    ``pattern`` is matched (via ``fnmatch``) against the tool's *permission
    target* string — e.g. the command for ``Bash`` or the path for ``Edit``.
    ``None`` means the rule applies to the whole tool regardless of arguments.
    """

    tool_name: str
    pattern: Optional[str]
    behavior: PermissionBehavior
    source: RuleSource = "session"

    def spec(self) -> str:
        """Render back to the ``Tool(pattern)`` text form (round-trips parsing)."""
        return self.tool_name if self.pattern is None else f"{self.tool_name}({self.pattern})"
