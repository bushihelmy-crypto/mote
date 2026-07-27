"""PermissionEngine — the runtime decision pipeline.

Combines mode + rules + per-tool self-checks into a single allow/deny outcome
for one tool call. Uses a layered permission-check flow, with bypass-immune
deny/ask rules and an interactive ``ask`` resolution
routed through the Role's ``request_approval`` capability.

The engine is intentionally infrastructure-only: it never imports tools. Callers
(``ToolExecutor``) supply the small facts it needs about a call — the
permission-target string, whether the tool mutates the filesystem, and the
optional decision a tool produced from its own ``check_permissions``.

Two orthogonal axes are applied in sequence:

  A) Approval decision (``_decide``) — *should we ask the user?*
     1. deny rule .................... deny            (bypass-immune)
     2. tool_check == deny ........... deny            (bypass-immune safety)
     3. ask rule ..................... -> ask          (bypass-immune)
     4. tool_check == ask ............ -> ask          (bypass-immune)
     5. mode == bypass ............... allow
     6. allow rule ................... allow
     7. tool_check == allow .......... allow
     8. mode == acceptEdits & mutates  allow
     9. mode == plan ................. deny            (read-only preview)
    10. mode == dontAsk ............. deny            (fail-closed, no prompt)
    11. default ..................... -> ask

  B) Sandbox boundary (``_apply_sandbox``) — *may it touch this path?*
     Applied only to allows that were NOT a fresh user approval (reason
     ``user``): a filesystem-mutating write outside the sandbox is escalated to
     the user (Codex's ``RequireEscalated`` flow) rather than hard-failed.
"""
from __future__ import annotations

import os
from typing import Awaitable, Callable, Optional

from mote.contracts.permissions import (
    ApprovalChoice,
    ApprovalReasonCode,
    ApprovalRequest,
    PermissionDecision,
    PermissionMode,
    PermissionRule,
)
from mote.runtime.tools.permission.rule_matcher import suggest_command_rule
from mote.runtime.tools.permission.rule_store import RuleStore
from mote.runtime.tools.permission.sandbox.guard import SandboxGuard

# An async callback that asks the human to approve a gated action and returns
# their structured decision. Supplied by the Role (``request_approval``
# capability); ``None`` when no interactive channel exists. The engine hands it
# a language-neutral :class:`ApprovalRequest` (never a prose string) and gets
# back one of the three :data:`ApprovalChoice` outcomes — display wording lives
# entirely in the human layer.
AskUser = Callable[[ApprovalRequest], Awaitable[ApprovalChoice]]


class PermissionEngine:
    """Evaluate tool calls against a mode + rule store + sandbox, prompting when needed."""

    def __init__(
        self,
        mode: PermissionMode,
        store: RuleStore,
        ask_user: Optional[AskUser] = None,
        sandbox: Optional[SandboxGuard] = None,
    ) -> None:
        self._mode = mode
        self._store = store
        self._ask_user = ask_user
        self._sandbox = sandbox

    async def check(
        self,
        tool_name: str,
        *,
        target: str = "",
        tool_check: Optional[PermissionDecision] = None,
        mutates_fs: bool = False,
        segments: Optional[list[str]] = None,
    ) -> PermissionDecision:
        """Resolve a single tool call to a terminal ``allow``/``deny`` decision.

        Any ``ask`` (from rules, the tool's self-check, the mode fallback, or a
        sandbox escalation) is resolved here by prompting the user, so the
        returned decision is never ``ask``.

        Args:
            tool_name: Primary name of the tool being invoked.
            target: The tool's permission-target string (command/path/...),
                matched against rule patterns. For a filesystem-mutating tool
                this is the write path checked against the sandbox.
            tool_check: Optional decision the tool produced from its own
                ``check_permissions`` (``allow``/``deny``/``ask``), or ``None``.
            mutates_fs: Whether the tool mutates the filesystem (drives the
                ``acceptEdits`` shortcut and the sandbox write check).
            segments: For shell-command tools, the command split into its
                independent segments (``a && b | c``). When supplied, rule
                resolution is folded strictest-wins across segments, and a
                single-segment command remembers an approval as a *prefix* rule.
        """
        decision = await self._decide(tool_name, target, tool_check, mutates_fs, segments)

        # Sandbox gate (axis B). Only narrows allows, and never re-questions a
        # write the user just approved this turn (reason "user").
        if decision.behavior == "allow" and mutates_fs and self._sandbox is not None and decision.reason.type != "user":
            decision = await self._apply_sandbox(tool_name, target, decision)

        return decision

    async def check_multi(
        self,
        tool_name: str,
        *,
        targets: list[str],
        tool_check: Optional[PermissionDecision] = None,
        mutates_fs: bool = False,
    ) -> PermissionDecision:
        """Resolve a call that touches *multiple* paths to one terminal decision.

        Each path is evaluated non-interactively against the same axis-A pipeline
        (rules + mode + ``tool_check``) and axis-B sandbox boundary as
        :meth:`check`, then folded **strictest-wins**:

          * any path denies                     -> deny (first reason)
          * else any path needs ask/escalation  -> ONE consolidated prompt
                                                    listing all such paths
          * else                                -> allow

        On a consolidated "always" grant, a session allow rule is remembered for
        each path that needed asking and each sandbox-escalation directory is
        widened, so the same multi-path call is not re-prompted.

        ``check()`` is intentionally left untouched — single-target tools keep
        their exact existing behavior.
        """
        if not targets:
            return await self.check(tool_name, target="", tool_check=tool_check, mutates_fs=mutates_fs)

        ask_paths: list[str] = []  # rule/default/tool_check ask
        escalation_paths: list[str] = []  # sandbox boundary violations
        reasons: list[str] = []

        for target in targets:
            decision = self._decide_static(tool_name, target, tool_check, mutates_fs)

            if decision.behavior == "deny":
                # Strictest wins immediately.
                return decision

            if decision.behavior == "ask":
                ask_paths.append(target)
                reasons.append(decision.message or decision.reason.detail)
                continue

            # allow — apply the sandbox boundary (axis B) just like check().
            if mutates_fs and self._sandbox is not None and decision.reason.type != "user":
                verdict = self._sandbox.check_write(target)
                if not verdict.allowed:
                    escalation_paths.append(target)
                    reasons.append(verdict.reason)

        if not ask_paths and not escalation_paths:
            return PermissionDecision.allow("multi", "all paths allowed")

        # One consolidated prompt for every path that needs confirmation.
        if self._ask_user is None:
            blocked = ", ".join(ask_paths + escalation_paths)
            return PermissionDecision.deny(
                "default",
                "multi-path approval required",
                message=(f"'{tool_name}' needs approval for {blocked} but no " f"interactive channel is available."),
            )

        pending = ask_paths + escalation_paths
        # A consolidated multi-path ask: escalation among them lifts the risk
        # band, and any concrete verdict text rides ``reason_detail`` verbatim.
        detail = "; ".join(r for r in reasons if r)
        request = ApprovalRequest(
            tool_name=tool_name,
            kind="escalation" if escalation_paths else "approval",
            target="\n  ".join(pending),
            paths=list(pending),
            risk="high" if escalation_paths else "medium",
            reason_code="sandbox" if escalation_paths else "default",
            reason_detail=detail,
        )
        choice = await self._ask_user(request)

        if choice == "deny":
            return PermissionDecision.deny(
                "user",
                "user denied",
                message=f"The user denied running '{tool_name}'.",
            )
        if choice == "allow_session":
            for path in ask_paths:
                self._store.add_session_rule(
                    PermissionRule(
                        tool_name=tool_name,
                        pattern=path or None,
                        behavior="allow",
                        source="session",
                    )
                )
            if self._sandbox is not None:
                for path in escalation_paths:
                    self._sandbox.add_session_root(os.path.dirname(path) or path)
        return PermissionDecision.allow("user", f"user approved ({choice})")

    def _decide_static(
        self,
        tool_name: str,
        target: str,
        tool_check: Optional[PermissionDecision],
        mutates_fs: bool,
    ) -> PermissionDecision:
        """Non-interactive twin of :meth:`_decide`: returns ``ask`` unresolved.

        Mirrors the 11-step axis-A precedence exactly but never prompts, so a
        caller (``check_multi``) can fold several paths before issuing a single
        consolidated approval prompt.
        """
        rule_behavior = self._store.resolve(tool_name, target)

        if rule_behavior == "deny":
            return PermissionDecision.deny(
                "rule",
                f"denied by rule for '{tool_name}'",
                message=f"'{tool_name}' is blocked by a deny rule.",
            )
        if tool_check is not None and tool_check.behavior == "deny":
            return tool_check

        if rule_behavior == "ask":
            return PermissionDecision.ask("rule", "an ask rule requires confirmation")
        if tool_check is not None and tool_check.behavior == "ask":
            return tool_check

        if self._mode == "bypass":
            return PermissionDecision.allow("mode", "bypass mode")

        if rule_behavior == "allow":
            return PermissionDecision.allow("rule", f"allowed by rule for '{tool_name}'")
        if tool_check is not None and tool_check.behavior == "allow":
            return tool_check

        if self._mode == "acceptEdits" and mutates_fs:
            return PermissionDecision.allow("mode", "acceptEdits mode")

        if self._mode == "plan":
            return PermissionDecision.deny(
                "mode",
                "plan mode",
                message=f"'{tool_name}' is blocked in plan mode (read-only preview).",
            )

        if self._mode == "dontAsk":
            return PermissionDecision.deny(
                "mode",
                "dontAsk mode",
                message=f"'{tool_name}' requires approval, denied in dontAsk mode.",
            )

        return PermissionDecision.ask("default", "this action needs your approval")

    # ------------------------------------------------------------------
    # Axis A — approval decision
    # ------------------------------------------------------------------

    async def _decide(
        self,
        tool_name: str,
        target: str,
        tool_check: Optional[PermissionDecision],
        mutates_fs: bool,
        segments: Optional[list[str]] = None,
    ) -> PermissionDecision:
        rule_behavior = self._resolve_rules(tool_name, target, segments)

        # 1-2. Bypass-immune denials.
        if rule_behavior == "deny":
            return PermissionDecision.deny(
                "rule", f"denied by rule for '{tool_name}'", message=f"'{tool_name}' is blocked by a deny rule."
            )
        if tool_check is not None and tool_check.behavior == "deny":
            return tool_check

        # 3-4. Bypass-immune asks.
        if rule_behavior == "ask":
            return await self._resolve_ask(tool_name, target, "rule", "an ask rule requires confirmation", segments)
        if tool_check is not None and tool_check.behavior == "ask":
            return await self._resolve_ask(
                tool_name, target, "tool_check", tool_check.message or "the tool requires confirmation", segments
            )

        # 5. Bypass mode: allow everything that wasn't deny/ask above.
        if self._mode == "bypass":
            return PermissionDecision.allow("mode", "bypass mode")

        # 6-7. Positive allows.
        if rule_behavior == "allow":
            return PermissionDecision.allow("rule", f"allowed by rule for '{tool_name}'")
        if tool_check is not None and tool_check.behavior == "allow":
            return tool_check

        # 8. acceptEdits: auto-approve filesystem-mutating tools.
        if self._mode == "acceptEdits" and mutates_fs:
            return PermissionDecision.allow("mode", "acceptEdits mode")

        # 9. plan: read-only preview — block anything not already allowed.
        if self._mode == "plan":
            return PermissionDecision.deny(
                "mode",
                "plan mode",
                message=f"'{tool_name}' is blocked in plan mode (read-only preview).",
            )

        # 10. dontAsk: never prompt — deny anything unresolved.
        if self._mode == "dontAsk":
            return PermissionDecision.deny(
                "mode",
                "dontAsk mode",
                message=f"'{tool_name}' requires approval, denied in dontAsk mode.",
            )

        # 11. default: ask the user.
        return await self._resolve_ask(tool_name, target, "default", "this action needs your approval", segments)

    def _resolve_rules(self, tool_name: str, target: str, segments: Optional[list[str]]) -> Optional[str]:
        """Rule behavior for a call: per-segment fold for commands, else single."""
        if segments is not None:
            return self._store.resolve_segments(tool_name, segments)
        return self._store.resolve(tool_name, target)

    # ------------------------------------------------------------------
    # Axis B — sandbox boundary
    # ------------------------------------------------------------------

    async def _apply_sandbox(self, tool_name: str, path: str, allowed: PermissionDecision) -> PermissionDecision:
        """Gate an allowed filesystem write against the sandbox boundary.

        Returns the original allow when the write is inside the boundary; on a
        violation, escalates to the user. With no interactive channel an
        escalation fails closed (deny). A "session" grant widens the sandbox via
        ``add_session_root`` so later writes under that directory pass silently.

        Only reached when the caller has already checked ``self._sandbox is not
        None`` (see the guard at the single call site), so narrow it here.
        """
        assert self._sandbox is not None, "_apply_sandbox called with no sandbox configured"
        verdict = self._sandbox.check_write(path)
        if verdict.allowed:
            return allowed

        if self._ask_user is None:
            return PermissionDecision.deny(
                "sandbox",
                verdict.reason,
                message=f"'{tool_name}' blocked by sandbox: {verdict.reason} (no channel to escalate).",
            )

        request = ApprovalRequest(
            tool_name=tool_name,
            kind="escalation",
            target=path,
            paths=[path],
            risk="high",
            reason_code="sandbox",
            reason_detail=verdict.reason,
        )
        choice = await self._ask_user(request)
        if choice == "deny":
            return PermissionDecision.deny(
                "sandbox",
                "user blocked sandbox escalation",
                message=f"The user blocked '{tool_name}' writing '{path}'.",
            )
        if choice == "allow_session":
            self._sandbox.add_session_root(os.path.dirname(path) or path)
        return PermissionDecision.allow("user", f"sandbox escalation approved ({choice})")

    # ------------------------------------------------------------------
    # Shared ask helper
    # ------------------------------------------------------------------

    async def _resolve_ask(
        self,
        tool_name: str,
        target: str,
        reason_type: str,
        reason: str,
        segments: Optional[list[str]] = None,
    ) -> PermissionDecision:
        """Prompt the user and turn their reply into allow/deny.

        Fail-closed: with no interactive channel, an ask becomes a deny. When the
        user picks "always", the rule named in the prompt is remembered for the
        session — a *prefix* rule for a single-segment command (so variations
        stop prompting), otherwise an exact-target rule.
        """
        if self._ask_user is None:
            return PermissionDecision.deny(
                reason_type,
                reason,
                message=f"'{tool_name}' needs approval but no interactive channel is available.",
            )

        rule, spec = self._suggested_allow_rule(tool_name, target, segments)
        # Map the internal reason_type to a language-neutral reason_code. A fixed
        # reason (ask rule / mode default) carries no detail — the display layer
        # localizes it from the code. A tool self-check ask carries the tool's
        # own (author-written English) message verbatim as ``reason_detail``.
        reason_code: ApprovalReasonCode
        if reason_type == "rule":
            reason_code, reason_detail = "ask_rule", ""
        elif reason_type == "tool_check":
            reason_code, reason_detail = "tool", reason
        else:
            reason_code, reason_detail = "default", ""
        request = ApprovalRequest(
            tool_name=tool_name,
            target=target,
            paths=[target] if target else [],
            reason_code=reason_code,
            reason_detail=reason_detail,
            suggestion=spec,
        )
        choice = await self._ask_user(request)

        if choice == "deny":
            return PermissionDecision.deny("user", "user denied", message=f"The user denied running '{tool_name}'.")
        if choice == "allow_session":
            # Remember exactly the rule shown in the prompt — no surprise grant.
            self._store.add_session_rule(rule)
        return PermissionDecision.allow("user", f"user approved ({choice})")

    def _suggested_allow_rule(
        self, tool_name: str, target: str, segments: Optional[list[str]]
    ) -> tuple[PermissionRule, str]:
        """The allow rule an "always" grant should add, plus its display spec.

        Single-segment shell commands get a prefix rule (``Bash(git commit:*)``)
        so the model can re-run variations without re-prompting. Compound
        commands and non-command tools fall back to an exact-target rule to
        avoid over-granting.
        """
        if segments is not None and len(segments) == 1:
            rule = suggest_command_rule(tool_name, segments[0])
            if rule is not None:
                return rule, f"{rule.tool_name}({rule.pattern})"
        pattern = target or None
        rule = PermissionRule(tool_name=tool_name, pattern=pattern, behavior="allow", source="session")
        spec = f"{tool_name}({pattern})" if pattern else tool_name
        return rule, spec
