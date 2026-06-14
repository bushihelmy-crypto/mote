"""Rule store — holds parsed rules and resolves the matching behavior.

Phase 1 wires two sources: the Role's declared config (``role``) and in-memory
``session`` rules added when a user picks "always" at an approval prompt. The
``project`` / ``local`` file-backed sources are reserved in the type system for
phase 2; no disk I/O happens here yet.

Resolution precedence is by behavior, not by source: a matching ``deny`` always
wins, then ``ask``, then ``allow``. This makes ``deny`` rules bypass-immune
(the engine consults the store before honoring ``bypass`` mode).
"""
from __future__ import annotations

from typing import Optional

from metagpt.common.schema import PermissionConfig
from metagpt.executor.permission.rule_matcher import parse_rule, rule_matches
from metagpt.executor.permission.types import PermissionBehavior, PermissionRule


class RuleStore:
    """A flat, ordered collection of permission rules with behavior precedence."""

    def __init__(self, rules: Optional[list[PermissionRule]] = None) -> None:
        self._rules: list[PermissionRule] = list(rules or [])

    @classmethod
    def from_config(cls, config: PermissionConfig) -> "RuleStore":
        """Build a store from a :class:`PermissionConfig`'s allow/deny/ask lists."""
        rules: list[PermissionRule] = []
        for spec in config.deny:
            rules.append(parse_rule(spec, "deny", source="role"))
        for spec in config.ask:
            rules.append(parse_rule(spec, "ask", source="role"))
        for spec in config.allow:
            rules.append(parse_rule(spec, "allow", source="role"))
        return cls(rules)

    def add_session_rule(self, rule: PermissionRule) -> None:
        """Remember a rule for the rest of the session (e.g. user chose 'always')."""
        self._rules.append(rule)

    def _has_match(self, behavior: PermissionBehavior, tool_name: str, target: str) -> bool:
        return any(
            r.behavior == behavior and rule_matches(r, tool_name, target) for r in self._rules
        )

    def resolve(self, tool_name: str, target: str) -> Optional[PermissionBehavior]:
        """Return the winning behavior for a call, or ``None`` if no rule matches.

        Precedence: deny > ask > allow.
        """
        if self._has_match("deny", tool_name, target):
            return "deny"
        if self._has_match("ask", tool_name, target):
            return "ask"
        if self._has_match("allow", tool_name, target):
            return "allow"
        return None
