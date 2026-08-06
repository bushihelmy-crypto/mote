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
from typing import Optional

from mote.contracts.authorization import PermissionDecision, PermissionMode, PermissionRule
from mote.runtime.tools.permission.rule_store import RuleStore
from mote.runtime.tools.permission.sandbox.guard import SandboxGuard


class PermissionEngine:
    """Evaluate permission and sandbox policy without performing interaction."""

    def __init__(
        self,
        mode: PermissionMode,
        store: RuleStore,
        sandbox: Optional[SandboxGuard] = None,
    ) -> None:
        self._mode = mode
        self._store = store
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
        decision = self._decide(tool_name, target, tool_check, mutates_fs, segments)

        # Sandbox gate (axis B). Only narrows allows, and never re-questions a
        # write the user just approved this turn (reason "user").
        if decision.behavior == "allow" and mutates_fs and self._sandbox is not None and decision.reason.type != "user":
            decision = self._apply_sandbox(tool_name, target, decision)

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

        pending = ask_paths + escalation_paths
        detail = "; ".join(r for r in reasons if r)
        return PermissionDecision.ask(
            "sandbox" if escalation_paths else "default",
            detail or "multi-path approval required",
            message="\n  ".join(pending),
        )

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

    def _decide(
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
            return PermissionDecision.ask("rule", "an ask rule requires confirmation")
        if tool_check is not None and tool_check.behavior == "ask":
            return PermissionDecision.ask("tool_check", tool_check.message or "the tool requires confirmation")

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
        return PermissionDecision.ask("default", "this action needs your approval")

    def _resolve_rules(self, tool_name: str, target: str, segments: Optional[list[str]]) -> Optional[str]:
        """Rule behavior for a call: per-segment fold for commands, else single."""
        if segments is not None:
            return self._store.resolve_segments(tool_name, segments)
        return self._store.resolve(tool_name, target)

    # ------------------------------------------------------------------
    # Axis B — sandbox boundary
    # ------------------------------------------------------------------

    def _apply_sandbox(self, tool_name: str, path: str, allowed: PermissionDecision) -> PermissionDecision:
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

        return PermissionDecision.ask("sandbox", verdict.reason, message=path)

    def remember_approved_session(self, tool_name: str, targets: tuple[str, ...], *, mutates_fs: bool) -> None:
        for target in targets or ("",):
            self._store.add_session_rule(
                PermissionRule(
                    tool_name=tool_name,
                    pattern=target or None,
                    behavior="allow",
                    source="session",
                )
            )
            if mutates_fs and self._sandbox is not None and target:
                self._sandbox.add_session_root(os.path.dirname(target) or target)
