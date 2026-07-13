"""Permission types — pure data, no logic.

Lives in ``common/schema`` (alongside ``tool_config.py``) and is kept
dependency-free (only stdlib typing/dataclasses) so any layer — tools, the
permission engine, the config schema, the hook layer — can import these as the
single source of truth without circular imports. Pure permission types stay
isolated from the permission engine; the runtime enforcement logic stays in
``mote.executor.permission``.

Two orthogonal concepts live here:
  * **Mode** — the coarse stance toward asking the user.
  * **Rule / Behavior** — fine-grained allow/deny/ask matching of a tool call.

The runtime decision pipeline (see ``executor/permission/engine.py``) combines
them into a single ``PermissionDecision``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional

# ---------------------------------------------------------------------------
# Enumerations (kept as Literals — no runtime enum machinery needed)
# ---------------------------------------------------------------------------

# Coarse stance, one of the following permission modes:
#   default      -> ask the user for anything not pre-authorized
#   acceptEdits  -> auto-allow filesystem-mutating tools (Edit/Write/...)
#   plan         -> read-only preview: deny anything that would act
#   bypass       -> allow everything EXCEPT bypass-immune deny/ask rules
#   dontAsk      -> never prompt; anything that would ask is denied (fail-closed)
PermissionMode = Literal["default", "acceptEdits", "plan", "bypass", "dontAsk"]

# The three terminal behaviors of a rule / decision.
PermissionBehavior = Literal["allow", "deny", "ask"]

# How long a user-granted approval lasts:
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
# Tool-derived facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionFacts:
    """The small bundle of tool-derived facts the permission engine needs.

    The engine deliberately never imports tools (see ``executor/permission``);
    instead the executor — which *does* own the tool — resolves these facts from
    the (possibly hook-rewritten) arguments and hands them to whoever evaluates
    the call. This is the single seam that lets the permission check run as a bus
    subscriber without the bus/subscriber layer learning anything about tools.

    * ``targets``    — the tool's *permission target* strings (command for Bash,
      path for Edit, ...); one entry per distinct target (``check_multi`` fans
      out over these).
    * ``mutates_fs`` — whether running the tool writes to the filesystem.
    * ``tool_check`` — a tool's self-declared :class:`PermissionDecision`
      (``check_permissions``), or ``None`` if the tool defers entirely to rules.
    * ``segments``   — sub-command segments for compound targets (e.g. a shell
      pipeline split into stages); ``None`` when the tool has no notion of them.
    """

    targets: list[str] = field(default_factory=list)
    mutates_fs: bool = False
    tool_check: Optional[PermissionDecision] = None
    segments: Optional[list[str]] = None


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


# ---------------------------------------------------------------------------
# Interactive approval — the structured request/decision round-trip
# ---------------------------------------------------------------------------

# What the human's decision maps to (the engine's own vocabulary — NOT display
# text). Returned by the ``request_approval`` capability.
#   allow_once    -> run this call only
#   allow_session -> run and remember for the session
#   deny          -> block this call
ApprovalChoice = Literal["allow_once", "allow_session", "deny"]

# Which kind of gate the human is answering. ``approval`` is a plain
# permission ask; ``escalation`` is a sandbox-boundary exception (the action is
# permitted by policy but would write outside the sandbox).
ApprovalKind = Literal["approval", "escalation"]

# A *stable, language-neutral* code for the fixed reason an approval was raised.
# The engine emits one of these; the human display layer (cli) maps it to a
# localized string. ``tool``/``sandbox`` carry a free-text ``reason_detail``
# instead (a tool's self-declared danger note / a sandbox verdict) — that detail
# is author-written English passed through verbatim, not localized.
#   ask_rule -> an ``ask`` permission rule matched
#   default  -> the mode fallback (nothing pre-authorized this call)
#   tool     -> the tool's own ``check_permissions`` asked (see reason_detail)
#   sandbox  -> a sandbox-boundary write exception (see reason_detail)
ApprovalReasonCode = Literal["ask_rule", "default", "tool", "sandbox"]


@dataclass(frozen=True)
class ApprovalRequest:
    """A gated tool call awaiting the human's structured approval decision.

    The semantic (not display) description of *what* needs approving. It carries
    only language-neutral facts — the tool name, the code artifact(s) at stake
    (``target``/``paths``, kept verbatim), the risk band, a fixed
    ``reason_code`` (+ optional free-text ``reason_detail`` for tool/sandbox
    reasons), and the session rule an "always" grant would add (``suggestion``).

    The human display layer renders these facts into localized wording via the
    i18n catalog; the engine never assembles a prose prompt string. This is the
    inbound half of the ``request_approval`` capability
    (``ApprovalRequest -> ApprovalChoice``) and mirrors the ``ApprovalRequested``
    ViewEvent that flows *down* to consumers.
    """

    tool_name: str
    kind: ApprovalKind = "approval"
    target: str = ""
    paths: List[str] = field(default_factory=list)
    risk: RiskLevel = "medium"
    reason_code: ApprovalReasonCode = "default"
    reason_detail: str = ""
    suggestion: str = ""
